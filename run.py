import asyncio
import importlib
import sys
import traceback
import warnings

import click

import config
from bot import FiresideBot
from cogs.utils.db import Table

# Asyncpg still explicitly passes `loop`, which is deprecated since python 3.8.
# We suppress this here because it's just unwanted noise.
warnings.simplefilter("ignore", category=DeprecationWarning)

try:
    # Try to load our blazing fast event handler.
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


def run_bot():
    loop = asyncio.get_event_loop()

    try:
        with warnings.catch_warnings():
            # Asyncpg still explicitly passes `loop`, which is deprecated since python 3.8.
            # We suppress this here because it's just unwanted noise.
            warnings.simplefilter("ignore")
            pool = loop.run_until_complete(Table.create_pool(config.postgresql, command_timeout=60))
    except Exception as e:
        print(f"Could not load PostgreSQL ({e}). Exiting.")
        return

    bot = FiresideBot(command_prefix=".")
    # Assign our bot pool properly
    bot.pool = pool
    bot.run()


@click.group(invoke_without_command=True, options_metavar="[options]")
@click.pass_context
def main(ctx):
    """Launches the bot."""
    if ctx.invoked_subcommand is None:
        run_bot()


@main.group(short_help="Database things", options_metavar="[options]")
def db():
    pass


@db.command(short_help="Initialises the databases for the bot", options_metavar="[options]")
@click.argument("cogs", nargs=-1, metavar="[cogs]")
@click.option("-q", "--quiet", help="less verbose output", is_flag=True)
def init(cogs, quiet):
    """This manages the migrations and database creation system for you."""

    run = asyncio.get_event_loop().run_until_complete
    try:
        run(Table.create_pool(config.postgresql))
    except Exception:
        click.echo(f"Could not create PostgreSQL connection pool.\n{traceback.format_exc()}", err=True)
        return

    if not cogs:
        cogs = config.autoload
    else:
        cogs = [f"cogs.{e}" if not e.startswith("cogs.") else e for e in cogs]

    for ext in cogs:
        try:
            importlib.import_module(ext)
        except Exception:
            click.echo(f"Could not load {ext}.\n{traceback.format_exc()}", err=True)
            return

    for table in Table.all_tables():
        try:
            created = run(table.create(verbose=not quiet, run_migrations=False))
        except Exception:
            click.echo(f"Could not create {table.__tablename__}.\n{traceback.format_exc()}", err=True)
        else:
            if created:
                click.echo(f"[{table.__module__}] Created {table.__tablename__}.")
            else:
                click.echo(f"[{table.__module__}] No work needed for {table.__tablename__}.")


@db.command(short_help="migrates the databases")
@click.argument("cog", nargs=1, metavar="[cog]")
@click.option("-q", "--quiet", help="less verbose output", is_flag=True)
@click.pass_context
def migrate(ctx, cog, quiet):
    """Update the migration file with the newest schema."""

    if not cog.startswith("cogs."):
        cog = f"cogs.{cog}"

    try:
        importlib.import_module(cog)
    except Exception as ext:
        click.echo(f"Could not load {ext}.\n{traceback.format_exc()}", err=True)
        return

    def work(table, *, invoked=False):
        try:
            actually_migrated = table.write_migration()
        except RuntimeError as e:
            click.echo(f"Could not migrate {table.__tablename__}: {e}", err=True)
            if not invoked:
                click.confirm("do you want to create the table?", abort=True)
                ctx.invoke(init, cogs=[cog], quiet=quiet)
                work(table, invoked=True)
            sys.exit(-1)
        else:
            if actually_migrated:
                click.echo(f"Successfully updated migrations for {table.__tablename__}.")
            else:
                click.echo(f"Found no changes for {table.__tablename__}.")

    for table in Table.all_tables():
        work(table)

    click.echo(f"Done migrating {cog}.")


async def apply_migration(cog, quiet, index, *, downgrade=False):
    try:
        pool = await Table.create_pool(config.postgresql)
    except Exception:
        click.echo(f"Could not create PostgreSQL connection pool.\n{traceback.format_exc()}", err=True)
        return

    if not cog.startswith("cogs."):
        cog = f"cogs.{cog}"

    try:
        importlib.import_module(cog)
    except Exception:
        click.echo(f"Could not load {cog}.\n{traceback.format_exc()}", err=True)
        return

    async with pool.acquire() as con:
        tr = con.transaction()
        await tr.start()
        for table in Table.all_tables():
            try:
                await table.migrate(index=index, downgrade=downgrade, verbose=not quiet, connection=con)
            except RuntimeError as e:
                click.echo(f"Could not migrate {table.__tablename__}: {e}", err=True)
                await tr.rollback()
                break
        else:
            await tr.commit()


@db.command(short_help="upgrades from a migration")
@click.argument("cog", nargs=1, metavar="[cog]")
@click.option("-q", "--quiet", help="less verbose output", is_flag=True)
@click.option("--index", help="the index to use", default=-1)
def upgrade(cog, quiet, index):
    """Runs an upgrade from a migration"""
    run = asyncio.get_event_loop().run_until_complete
    run(apply_migration(cog, quiet, index))


@db.command(short_help="downgrades from a migration")
@click.argument("cog", nargs=1, metavar="[cog]")
@click.option("-q", "--quiet", help="less verbose output", is_flag=True)
@click.option("--index", help="the index to use", default=-1)
def downgrade(cog, quiet, index):
    """Runs an downgrade from a migration"""
    run = asyncio.get_event_loop().run_until_complete
    run(apply_migration(cog, quiet, index, downgrade=True))


async def remove_databases(pool, cog, quiet):
    async with pool.acquire() as con:
        tr = con.transaction()
        await tr.start()
        for table in Table.all_tables():
            try:
                await table.drop(verbose=not quiet, connection=con)
            except RuntimeError as e:
                click.echo(f"Could not drop {table.__tablename__}: {e}", err=True)
                await tr.rollback()
                break
            else:
                click.echo(f"Dropped {table.__tablename__}.")
        else:
            await tr.commit()
            click.echo(f"successfully removed {cog} tables.")


@db.command(short_help="removes a cog's table", options_metavar="[options]")
@click.argument("cog", metavar="<cog>")
@click.option("-q", "--quiet", help="less verbose output", is_flag=True)
def drop(cog, quiet):
    """This removes a database and all its migrations.
    You must be pretty sure about this before you do it,
    as once you do it there"s no coming back.
    Also note that the name must be the database name, not
    the cog name.
    """

    run = asyncio.get_event_loop().run_until_complete
    click.confirm("do you really want to do this?", abort=True)

    try:
        pool = run(Table.create_pool(config.postgresql))
    except Exception:
        click.echo(f"Could not create PostgreSQL connection pool.\n{traceback.format_exc()}", err=True)
        return

    if not cog.startswith("cogs."):
        cog = f"cogs.{cog}"

    try:
        importlib.import_module(cog)
    except Exception:
        click.echo(f"Could not load {cog}.\n{traceback.format_exc()}", err=True)
        return

    run(remove_databases(pool, cog, quiet))


if __name__ == "__main__":
    main()

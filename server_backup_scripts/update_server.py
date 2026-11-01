#!/usr/bin/env python3

import click
from loguru import logger


@click.command()
@logger.catch(reraise=True)
def main():
    raise click.ClickException("The update service is not implemented yet")


class Update:
    def vaultwarden(self):
        pass

    def apt(self):
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import click
from loguru import logger


@click.command()
@logger.catch
def main():
    logger.info("Running update service")


class Update:
    def vaultwarden(self):
        pass

    def apt(self):
        pass


if __name__ == "__main__":
    main()

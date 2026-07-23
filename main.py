from pathlib import Path

from core.collector import Collector
from core.config import load_config
from gnss import create_gnss_receiver
from modems import create_modem


def main():
    config = load_config(Path("config.toml"))
    modem = create_modem(config.modem)
    gnss_receiver = create_gnss_receiver(config.gnss_receiver)
    collector = Collector(modem, gnss_receiver, config.collector)
    collector.collect()


if __name__ == "__main__":
    pass
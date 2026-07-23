from gnss.gnss_reciever import GNSSReceiver, GNSSFix


class NMEASerial(GNSSReceiver):
    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def read_fix(self) -> GNSSFix | None:
        raise NotImplementedError
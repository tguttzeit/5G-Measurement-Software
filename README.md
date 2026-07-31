# 5G Measurement Software

## Prerequisites

Build quectel-CM:

```bash
cd quectel-CM
make clean
make
```

For the optional latency tests (`[latency_test]` in `config.toml`), install flent and the tools
it drives on the device:

```bash
sudo apt install flent netperf python3-numpy
```

The tests also need a counterpart on the test server: `netperf`'s `netserver` for the load test,
and a host that answers pings for the baseline. Until that side is confirmed, leave
`[latency_test] enabled = false`.

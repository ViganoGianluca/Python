#!/usr/bin/env python3
"""Analizzatore e ottimizzatore Wi-Fi (solo libreria standard).

Esegue, sul computer collegato alla rete:
  1. scansione delle reti Wi-Fi vicine (Linux/nmcli, macOS/system_profiler,
     Windows/netsh, anche in italiano);
  2. calcolo della congestione per canale e scelta del canale migliore
     a 2,4 GHz e a 5 GHz;
  3. test di latenza, jitter e perdita pacchetti verso router e Internet;
  4. elenco di consigli concreti da applicare sul router.

Uso:
    python3 wifi_optimizer.py            # analisi completa
    python3 wifi_optimizer.py --no-ping  # solo scansione radio
    python3 wifi_optimizer.py --json     # output in JSON
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass, field

CHANNELS_24 = [1, 6, 11]  # gli unici canali a 2,4 GHz che non si sovrappongono
CHANNELS_5_NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165]
CHANNELS_5_DFS = [52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144]


# --------------------------------------------------------------------------- #
# Modello dati
# --------------------------------------------------------------------------- #
@dataclass
class Network:
    ssid: str
    bssid: str
    channel: int
    signal_dbm: float
    band: str = ""
    security: str = ""
    in_use: bool = False

    def __post_init__(self) -> None:
        if not self.band:
            self.band = band_for_channel(self.channel)


@dataclass
class PingResult:
    host: str
    sent: int
    received: int
    avg_ms: float | None = None
    jitter_ms: float | None = None
    max_ms: float | None = None

    @property
    def loss_pct(self) -> float:
        return 100.0 * (self.sent - self.received) / self.sent if self.sent else 100.0


@dataclass
class ChannelScore:
    channel: int
    score: float          # interferenza relativa (più basso = meglio)
    networks: int         # reti che interferiscono
    dfs: bool = False


@dataclass
class Report:
    networks: list[Network]
    current: Network | None
    best_24: list[ChannelScore]
    best_5: list[ChannelScore]
    pings: list[PingResult] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def band_for_channel(channel: int, freq_mhz: int | None = None) -> str:
    if freq_mhz:
        if freq_mhz >= 5925:
            return "6"
        return "5" if freq_mhz >= 4900 else "2.4"
    return "2.4" if 1 <= channel <= 14 else "5"


def percent_to_dbm(pct: float) -> float:
    """Conversione approssimata qualità % -> dBm (convenzione di nmcli/Windows)."""
    return max(-100.0, min(-30.0, pct / 2.0 - 100.0))


def signal_quality(dbm: float) -> str:
    if dbm >= -50:
        return "eccellente"
    if dbm >= -60:
        return "ottimo"
    if dbm >= -67:
        return "buono"
    if dbm >= -75:
        return "scarso"
    return "pessimo"


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Parser delle scansioni
# --------------------------------------------------------------------------- #
def _split_nmcli(line: str) -> list[str]:
    """Divide una riga `nmcli -t` sui ':' non preceduti da backslash."""
    parts, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def parse_nmcli(output: str) -> list[Network]:
    """Formato: IN-USE:SSID:BSSID:CHAN:FREQ:SIGNAL:SECURITY (nmcli -t)."""
    nets = []
    for line in output.splitlines():
        f = _split_nmcli(line.strip())
        if len(f) < 7:
            continue
        in_use, ssid, bssid, chan, freq, sig, sec = f[:7]
        try:
            channel = int(chan)
            freq_mhz = int(re.sub(r"\D", "", freq) or 0) or None
            dbm = percent_to_dbm(float(sig))
        except ValueError:
            continue
        nets.append(Network(ssid or "<nascosta>", bssid.upper(), channel, dbm,
                            band_for_channel(channel, freq_mhz), sec, in_use.strip() == "*"))
    return nets


_NETSH_KEYS = {
    "ssid": re.compile(r"^SSID\s+\d+\s*:\s*(.*)$", re.I),
    "bssid": re.compile(r"^BSSID\s+\d+\s*:\s*(\S+)", re.I),
    "signal": re.compile(r"^(?:Signal|Segnale)\s*:\s*(\d+)\s*%", re.I),
    "channel": re.compile(r"^(?:Channel|Canale)\s*:\s*(\d+)", re.I),
    "auth": re.compile(r"^(?:Authentication|Autenticazione)\s*:\s*(.*)$", re.I),
}


def parse_netsh(output: str) -> list[Network]:
    """`netsh wlan show networks mode=bssid` (inglese o italiano)."""
    nets: list[Network] = []
    ssid, auth, bssid, sig = "", "", None, None

    for raw in output.splitlines():
        line = raw.strip()
        if m := _NETSH_KEYS["ssid"].match(line):
            ssid, auth, bssid, sig = m.group(1).strip() or "<nascosta>", "", None, None
        elif m := _NETSH_KEYS["auth"].match(line):
            auth = m.group(1).strip()
        elif m := _NETSH_KEYS["bssid"].match(line):
            bssid, sig = m.group(1).upper(), None
        elif m := _NETSH_KEYS["signal"].match(line):
            sig = float(m.group(1))
        elif (m := _NETSH_KEYS["channel"].match(line)) and bssid and sig is not None:
            nets.append(Network(ssid, bssid, int(m.group(1)), percent_to_dbm(sig), security=auth))
            bssid = None
    return nets


def parse_netsh_interface(output: str) -> dict[str, str]:
    """`netsh wlan show interfaces` -> dizionario con ssid/bssid/channel/signal."""
    info: dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.strip()
        if m := re.match(r"^SSID\s*:\s*(.+)$", line, re.I):
            info["ssid"] = m.group(1).strip()
        elif m := re.match(r"^(?:AP\s+)?BSSID\s*:\s*(\S+)", line, re.I):
            info["bssid"] = m.group(1).upper()
        elif m := _NETSH_KEYS["channel"].match(line):
            info["channel"] = m.group(1)
        elif m := _NETSH_KEYS["signal"].match(line):
            info["signal"] = m.group(1)
    return info


def _mac_channel(value: str) -> tuple[int, str]:
    m = re.match(r"(\d+)(?:\s*\((\d+)GHz)?", str(value))
    if not m:
        return 0, ""
    ch = int(m.group(1))
    ghz = m.group(2)
    band = {"2": "2.4", "5": "5", "6": "6"}.get(ghz or "", band_for_channel(ch))
    return ch, band


def _mac_dbm(value: str) -> float:
    m = re.search(r"(-\d+)\s*dBm", str(value))
    return float(m.group(1)) if m else -100.0


def parse_system_profiler(output: str) -> list[Network]:
    """`system_profiler SPAirPortDataType -json` (macOS 14+)."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    nets: list[Network] = []
    for block in data.get("SPAirPortDataType", []):
        for iface in block.get("spairport_airport_interfaces", []):
            cur = iface.get("spairport_current_network_information")
            entries = [(cur, True)] if cur else []
            entries += [(n, False) for n in iface.get("spairport_airport_other_local_wireless_networks", [])]
            for n, in_use in entries:
                ch, band = _mac_channel(n.get("spairport_network_channel", ""))
                if not ch:
                    continue
                nets.append(Network(
                    n.get("_name", "<nascosta>"),
                    n.get("spairport_network_bssid", ""),
                    ch,
                    _mac_dbm(n.get("spairport_signal_noise", "")),
                    band,
                    n.get("spairport_security_mode", "").replace("spairport_security_mode_", ""),
                    in_use,
                ))
    return nets


def scan(system: str | None = None) -> list[Network]:
    system = system or platform.system()
    if system == "Linux":
        if not shutil.which("nmcli"):
            raise RuntimeError("nmcli non trovato: installa NetworkManager.")
        _run(["nmcli", "dev", "wifi", "rescan"])
        out = _run(["nmcli", "-t", "-f", "IN-USE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY", "dev", "wifi", "list"])
        return parse_nmcli(out)
    if system == "Darwin":
        return parse_system_profiler(_run(["system_profiler", "SPAirPortDataType", "-json"], timeout=60))
    if system == "Windows":
        nets = parse_netsh(_run(["netsh", "wlan", "show", "networks", "mode=bssid"]))
        cur = parse_netsh_interface(_run(["netsh", "wlan", "show", "interfaces"]))
        for n in nets:
            if cur.get("bssid") and n.bssid == cur["bssid"]:
                n.in_use = True
        return nets
    raise RuntimeError(f"Sistema operativo non supportato: {system}")


# --------------------------------------------------------------------------- #
# Analisi dei canali
# --------------------------------------------------------------------------- #
def _power(dbm: float) -> float:
    """Potenza lineare relativa (1.0 = -50 dBm)."""
    return 10 ** ((dbm + 50) / 10)


def score_channels(networks: list[Network], exclude_bssids: set[str] = frozenset()) -> tuple[list[ChannelScore], list[ChannelScore]]:
    others = [n for n in networks if n.bssid not in exclude_bssids]

    best_24 = []
    for c in CHANNELS_24:
        score, count = 0.0, 0
        for n in others:
            if n.band != "2.4":
                continue
            overlap = max(0.0, 1 - abs(c - n.channel) / 5)  # 20 MHz ≈ 5 canali
            if overlap > 0:
                score += overlap * _power(n.signal_dbm)
                count += 1
        best_24.append(ChannelScore(c, round(score, 4), count))

    best_5 = []
    for c in CHANNELS_5_NON_DFS + CHANNELS_5_DFS:
        hits = [n for n in others if n.band == "5" and n.channel == c]
        best_5.append(ChannelScore(c, round(sum(_power(n.signal_dbm) for n in hits), 4),
                                   len(hits), dfs=c in CHANNELS_5_DFS))

    best_24.sort(key=lambda s: (s.score, s.networks))
    best_5.sort(key=lambda s: (s.score, s.networks, s.dfs))
    return best_24, best_5


# --------------------------------------------------------------------------- #
# Test di rete
# --------------------------------------------------------------------------- #
_PING_TIME = re.compile(r"(?:time|durata|tempo)\s*[=<]\s*([\d.,]+)\s*ms", re.I)


def parse_ping(host: str, output: str, sent: int) -> PingResult:
    times = [float(t.replace(",", ".")) for t in _PING_TIME.findall(output)]
    res = PingResult(host, sent, len(times))
    if times:
        res.avg_ms = round(statistics.fmean(times), 2)
        res.max_ms = round(max(times), 2)
        diffs = [abs(a - b) for a, b in zip(times, times[1:])]
        res.jitter_ms = round(statistics.fmean(diffs), 2) if diffs else 0.0
    return res


def ping(host: str, count: int = 20, system: str | None = None) -> PingResult:
    system = system or platform.system()
    if system == "Windows":
        cmd = ["ping", "-n", str(count), host]
    else:
        cmd = ["ping", "-c", str(count), "-i", "0.2", host]
    return parse_ping(host, _run(cmd, timeout=count * 2 + 10), count)


def default_gateway(system: str | None = None) -> str | None:
    system = system or platform.system()
    ipv4 = r"(\d{1,3}(?:\.\d{1,3}){3})"
    if system == "Linux":
        m = re.search(r"default via " + ipv4, _run(["ip", "route", "show", "default"]))
    elif system == "Darwin":
        m = re.search(r"gateway:\s*" + ipv4, _run(["route", "-n", "get", "default"]))
    elif system == "Windows":
        m = re.search(r"(?:Default Gateway|Gateway predefinito)[ .]*:\s*" + ipv4, _run(["ipconfig"]))
    else:
        m = None
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Raccomandazioni
# --------------------------------------------------------------------------- #
def recommend(networks: list[Network], current: Network | None,
              best_24: list[ChannelScore], best_5: list[ChannelScore],
              pings: list[PingResult], gateway: str | None = None) -> list[str]:
    tips: list[str] = []

    if current is None:
        tips.append("Non risulti connesso a una rete Wi-Fi: collegati e rilancia lo script.")
    else:
        q = signal_quality(current.signal_dbm)
        if current.signal_dbm < -67:
            tips.append(
                f"Segnale {q} ({current.signal_dbm:.0f} dBm): avvicinati al router, mettilo in posizione "
                "centrale e rialzata, lontano da muri spessi, metallo, specchi, acquari e microonde; "
                "se la casa è grande valuta un sistema mesh o un access point via cavo.")

        same_ssid = [n for n in networks if n.ssid == current.ssid and n.bssid != current.bssid]
        if current.band == "2.4" and any(n.band in ("5", "6") and n.signal_dbm >= -70 for n in same_ssid):
            tips.append("Sei sulla banda 2,4 GHz ma la tua rete è disponibile anche a 5 GHz con buon segnale: "
                        "attiva il band steering sul router o connettiti alla rete 5 GHz.")

        own = {n.bssid for n in networks if n.ssid == current.ssid}
        if current.band == "2.4":
            if current.channel not in CHANNELS_24:
                tips.append(f"Il router usa il canale {current.channel} a 2,4 GHz, che si sovrappone ai vicini: "
                            "usa solo 1, 6 o 11.")
            cur_score = next((s for s in best_24 if s.channel == current.channel), None)
            top = best_24[0]
            if top.channel != current.channel and (cur_score is None or top.score < cur_score.score * 0.5):
                tips.append(f"Imposta manualmente il canale {top.channel} a 2,4 GHz "
                            f"(interferenza {top.networks} reti) invece del canale {current.channel}.")
            tips.append("A 2,4 GHz usa larghezza di canale 20 MHz: 40 MHz aumenta le interferenze.")
        elif current.band == "5":
            cur_hits = [n for n in networks if n.band == "5" and n.channel == current.channel and n.bssid not in own]
            top = best_5[0]
            if cur_hits and top.channel != current.channel:
                dfs = " (canale DFS: verifica che i dispositivi lo supportino)" if top.dfs else ""
                tips.append(f"Altre {len(cur_hits)} reti usano il tuo canale {current.channel} a 5 GHz: "
                            f"passa al canale {top.channel}{dfs}.")
            tips.append("A 5 GHz usa 80 MHz se ci sono pochi vicini, altrimenti 40 MHz per maggiore stabilità.")

    crowded = sum(1 for n in networks if n.band == "2.4" and n.signal_dbm > -80)
    if crowded >= 10:
        tips.append(f"La banda 2,4 GHz è affollata ({crowded} reti percepibili): sposta su 5 GHz "
                    "tutti i dispositivi che lo supportano e lascia la 2,4 GHz a domotica e IoT.")

    for p in pings:
        label = "router" if p.host == gateway else p.host
        if p.received == 0:
            tips.append(f"Nessuna risposta da {label}: verifica la connessione (o il ping è bloccato).")
            continue
        if p.loss_pct > 1:
            tips.append(f"Perdita pacchetti del {p.loss_pct:.0f}% verso {label}: tipico di interferenze o segnale debole.")
        if p.host == gateway:
            if p.avg_ms and p.avg_ms > 10:
                tips.append(f"Latenza verso il router alta ({p.avg_ms} ms, normale < 5 ms): "
                            "problema radio locale (canale congestionato, distanza o ostacoli).")
            if p.jitter_ms and p.jitter_ms > 5:
                tips.append(f"Jitter verso il router alto ({p.jitter_ms} ms): disturba videochiamate e giochi; "
                            "per PC fissi, TV e console usa il cavo Ethernet.")
        elif p.avg_ms and p.avg_ms > 60:
            tips.append(f"Latenza verso Internet elevata ({p.avg_ms} ms): se il router risponde bene il "
                        "collo di bottiglia è la linea; attiva SQM/QoS sul router o contatta il provider.")

    tips += [
        "Aggiorna il firmware del router e riavvialo periodicamente.",
        "Usa WPA3 (o WPA2-AES) e disattiva WPS e WEP/TKIP: oltre alla sicurezza, TKIP limita la velocità a 54 Mbit/s.",
        "Disattiva le reti ospiti e le bande inutilizzate per ridurre il traffico di gestione in aria.",
    ]
    return tips


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def analyze(networks: list[Network], pings: list[PingResult], gateway: str | None = None) -> Report:
    current = next((n for n in networks if n.in_use), None)
    own = {n.bssid for n in networks if current and n.ssid == current.ssid}
    best_24, best_5 = score_channels(networks, own)
    return Report(networks, current, best_24, best_5, pings,
                  recommend(networks, current, best_24, best_5, pings, gateway))


def print_report(r: Report) -> None:
    print("=" * 64)
    print(" ANALISI RETE WI-FI")
    print("=" * 64)
    if r.current:
        c = r.current
        print(f"\nConnesso a: {c.ssid}  [{c.bssid}]")
        print(f"  Banda {c.band} GHz, canale {c.channel}, segnale {c.signal_dbm:.0f} dBm "
              f"({signal_quality(c.signal_dbm)})")

    print(f"\nReti rilevate: {len(r.networks)}")
    print(f"  {'SSID':<28}{'Banda':>6}{'Can.':>6}{'dBm':>6}")
    for n in sorted(r.networks, key=lambda n: -n.signal_dbm)[:25]:
        mark = "*" if n.in_use else " "
        print(f"{mark} {n.ssid[:27]:<28}{n.band:>6}{n.channel:>6}{n.signal_dbm:>6.0f}")

    print("\nCanali consigliati 2,4 GHz (dal migliore):")
    for s in r.best_24:
        print(f"  canale {s.channel:>3}: interferenza {s.score:8.3f}  ({s.networks} reti)")
    print("\nCanali consigliati 5 GHz (primi 5):")
    for s in r.best_5[:5]:
        print(f"  canale {s.channel:>3}: interferenza {s.score:8.3f}  ({s.networks} reti)"
              f"{'  [DFS]' if s.dfs else ''}")

    if r.pings:
        print("\nTest di rete:")
        for p in r.pings:
            avg = f"{p.avg_ms} ms" if p.avg_ms is not None else "n/d"
            jit = f"{p.jitter_ms} ms" if p.jitter_ms is not None else "n/d"
            print(f"  {p.host:<16} media {avg:<10} jitter {jit:<10} perdita {p.loss_pct:.0f}%")

    print("\nCOSA FARE:")
    for i, t in enumerate(r.recommendations, 1):
        print(f"  {i}. {t}")
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Analizza e ottimizza la rete Wi-Fi.")
    ap.add_argument("--no-ping", action="store_true", help="salta i test di latenza")
    ap.add_argument("--count", type=int, default=20, help="numero di ping per host (default 20)")
    ap.add_argument("--host", default="1.1.1.1", help="host Internet da testare (default 1.1.1.1)")
    ap.add_argument("--json", action="store_true", help="stampa il report in JSON")
    args = ap.parse_args(argv)

    try:
        networks = scan()
    except RuntimeError as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 1
    if not networks:
        print("Nessuna rete trovata. Il Wi-Fi è acceso? Su macOS concedi a Terminale i permessi "
              "di Localizzazione; su Windows abilita i servizi di localizzazione.", file=sys.stderr)

    pings: list[PingResult] = []
    gw = None
    if not args.no_ping:
        gw = default_gateway()
        for host in filter(None, [gw, args.host]):
            pings.append(ping(host, args.count))

    report = analyze(networks, pings, gw)
    if args.json:
        data = asdict(report)
        for p, d in zip(report.pings, data["pings"]):
            d["loss_pct"] = p.loss_pct
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

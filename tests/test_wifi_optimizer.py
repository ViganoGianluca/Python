import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wifi_optimizer as w  # noqa: E402

NMCLI = r"""*:CasaMia:AA\:BB\:CC\:DD\:EE\:01:3:2422 MHz:70:WPA2
 :CasaMia:AA\:BB\:CC\:DD\:EE\:02:44:5220 MHz:64:WPA2
 :Vicino1:11\:22\:33\:44\:55\:66:1:2412 MHz:90:WPA2
 :Vicino2:11\:22\:33\:44\:55\:77:6:2437 MHz:40:WPA3
 ::11\:22\:33\:44\:55\:88:36:5180 MHz:50:WPA2
"""

NETSH_IT = """
SSID 1 : CasaMia
    Tipo di rete            : Infrastruttura
    Autenticazione          : WPA2-Personal
    BSSID 1                 : aa:bb:cc:dd:ee:01
         Segnale            : 80%
         Tipo frequenza radio : 802.11ax
         Canale             : 6
    BSSID 2                 : aa:bb:cc:dd:ee:02
         Segnale            : 60%
         Canale             : 36
SSID 2 : Vicino
    Autenticazione          : WPA3-Personal
    BSSID 1                 : 11:22:33:44:55:66
         Signal             : 50%
         Channel            : 11
"""

MAC = {
    "SPAirPortDataType": [{
        "spairport_airport_interfaces": [{
            "spairport_current_network_information": {
                "_name": "CasaMia",
                "spairport_network_channel": "6 (2GHz, 20MHz)",
                "spairport_signal_noise": "-72 dBm / -92 dBm",
            },
            "spairport_airport_other_local_wireless_networks": [
                {"_name": "CasaMia", "spairport_network_channel": "149 (5GHz, 80MHz)",
                 "spairport_signal_noise": "-60 dBm / -92 dBm"},
                {"_name": "Vicino", "spairport_network_channel": "6 (2GHz, 20MHz)",
                 "spairport_signal_noise": "-55 dBm / -92 dBm"},
            ],
        }]
    }]
}

PING_LINUX = """64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=2.10 ms
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=4.10 ms
64 bytes from 192.168.1.1: icmp_seq=3 ttl=64 time=3.10 ms
"""
PING_WIN_IT = """Risposta da 192.168.1.1: byte=32 durata=3ms TTL=64
Risposta da 192.168.1.1: byte=32 durata<1ms TTL=64
Richiesta scaduta.
"""


class ParserTests(unittest.TestCase):
    def test_nmcli(self):
        nets = w.parse_nmcli(NMCLI)
        self.assertEqual(len(nets), 5)
        cur = nets[0]
        self.assertTrue(cur.in_use)
        self.assertEqual(cur.bssid, "AA:BB:CC:DD:EE:01")
        self.assertEqual((cur.channel, cur.band), (3, "2.4"))
        self.assertEqual(cur.signal_dbm, -65)
        self.assertEqual(nets[1].band, "5")
        self.assertEqual(nets[4].ssid, "<nascosta>")

    def test_netsh_italian_and_english(self):
        nets = w.parse_netsh(NETSH_IT)
        self.assertEqual([(n.ssid, n.channel) for n in nets],
                         [("CasaMia", 6), ("CasaMia", 36), ("Vicino", 11)])
        self.assertEqual(nets[0].security, "WPA2-Personal")
        self.assertEqual(nets[2].signal_dbm, -75)

    def test_system_profiler(self):
        nets = w.parse_system_profiler(json.dumps(MAC))
        self.assertEqual(len(nets), 3)
        self.assertTrue(nets[0].in_use)
        self.assertEqual((nets[0].channel, nets[0].band, nets[0].signal_dbm), (6, "2.4", -72))
        self.assertEqual(nets[1].band, "5")

    def test_ping_parsing(self):
        r = w.parse_ping("192.168.1.1", PING_LINUX, 3)
        self.assertEqual((r.received, r.avg_ms, r.jitter_ms, r.loss_pct), (3, 3.1, 1.5, 0.0))
        r = w.parse_ping("192.168.1.1", PING_WIN_IT, 3)
        self.assertEqual(r.received, 2)
        self.assertAlmostEqual(r.loss_pct, 33.33, places=1)


class AnalysisTests(unittest.TestCase):
    def test_channel_scoring_prefers_free_channel(self):
        nets = [w.Network("A", "1", 1, -45), w.Network("B", "2", 6, -50), w.Network("C", "3", 36, -60)]
        b24, b5 = w.score_channels(nets)
        self.assertEqual(b24[0].channel, 11)
        self.assertNotEqual(b5[0].channel, 36)
        self.assertFalse(b5[0].dfs)

    def test_recommendations(self):
        r = w.analyze(w.parse_nmcli(NMCLI), [])
        text = " ".join(r.recommendations)
        self.assertIn("1, 6 o 11", text)          # canale 3 si sovrappone
        self.assertIn("5 GHz", text)              # band steering
        self.assertEqual(r.best_24[0].channel, 11)

    def test_bad_gateway_latency(self):
        p = w.PingResult("192.168.1.1", 20, 18, avg_ms=25, jitter_ms=12, max_ms=80)
        tips = w.recommend([], None, *w.score_channels([]), [p], gateway="192.168.1.1")
        text = " ".join(tips)
        self.assertIn("Perdita pacchetti", text)
        self.assertIn("Latenza verso il router", text)
        self.assertIn("Jitter", text)


if __name__ == "__main__":
    unittest.main()

# Wi-Fi Optimizer

Script Python (solo libreria standard, Python 3.10+) che analizza la tua rete Wi-Fi e ti dice cosa cambiare sul router.

## Cosa fa
- Scansiona le reti vicine (Linux `nmcli`, macOS `system_profiler`, Windows `netsh`, anche in italiano)
- Calcola l'interferenza per canale e consiglia il canale migliore a 2,4 GHz (1/6/11) e a 5 GHz
- Misura latenza, jitter e perdita pacchetti verso il router e verso Internet
- Stampa un elenco di azioni concrete (canale, larghezza di banda, posizione del router, band steering, sicurezza…)

## Uso
```bash
python3 wifi_optimizer.py            # analisi completa
python3 wifi_optimizer.py --no-ping  # solo scansione radio
python3 wifi_optimizer.py --json     # output JSON
```
Esegui lo script sul computer collegato al Wi-Fi, idealmente dalla stanza in cui hai problemi.
Su macOS concedi a Terminale l'accesso alla Localizzazione (serve per vedere i nomi delle reti); su Windows attiva i servizi di localizzazione.

## Test
```bash
python3 -m unittest discover -s tests
```

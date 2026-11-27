# Security policy

Narzędzie do zamkniętej sieci zakładowej. Nie wystawiaj go do internetu. Zgłoszenia podatności:
GitHub → Security → *Report a vulnerability* (nie otwieraj publicznego issue). Odpowiadam w ciągu 7 dni.

- Panel domyślnie nasłuchuje na 127.0.0.1. Dostęp z sieci: ustaw `host` w `config.json` oraz **obowiązkowo** `MONITOR_USER` i `MONITOR_PASSWORD` (HTTP Basic — używaj tylko w zaufanej sieci albo za proxy z TLS).
- Hasła do robotów: wyłącznie zmienne środowiskowe `ROBOT_<ID>_PASSWORD`. Fabryczne hasło robota trzeba zmienić; przy starcie w trybie `opcua` program ostrzega o haśle pustym lub fabrycznym.
- Roboty trzymaj w osobnym VLAN-ie. Program tylko czyta z robotów i niczego do nich nie wysyła.

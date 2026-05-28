# DHMZ_EYWA

Automatizacija čitanja podataka iz tablice sa DHMZ web stranice

## Pokretanje


Povežite se na svoju EYWA instancu:
```bash
eywa connect <YOUR_EYWA_URL>
```


Instalirajte potrebne pakete:
```bash
pip install -r requirements.txt
```


Pokrenite DHMZ sync robota lokalno:
```bash
eywa run -c 'python dhmz_eywa_cache_opisano.py'
```

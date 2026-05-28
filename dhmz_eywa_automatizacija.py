"""
DHMZ dnevni scraper + EYWA GraphQL sync za DVIJE tablice: DhmzStanica i DhmzMjerenja.

Namjena:
- Program pokrećemo jednom dnevno.
- Pročita DHMZ mjerenja za svaki sat od 00 do trenutnog sata.
- Ako želimo uvijek pokupiti svih 24 sata, postavi SINKRONIZIRAJ_SVA_24_SATA = True.
- Svaki sat se čita s URL-a oblika:
  https://meteo.hr/podaci.php?section=podaci_vrijeme&param=hrvatska1_n&sat=HH
- Podaci se spremaju u:
  1) DhmzStanica
  2) DhmzMjerenja

Upsert logika:
- DhmzStanica se traži po lokacija.
- DhmzMjerenja se traži po postaja + date_time.
"""

import asyncio
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import eywa

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


DHMZ_OSNOVNI_URL = "https://meteo.hr/podaci.php"
DHMZ_SEKCIJA = "podaci_vrijeme"
DHMZ_PARAMETAR = "hrvatska1_n"
ZAGREB_VREMENSKA_ZONA = ZoneInfo("Europe/Zagreb")

# Ako je True, čita sve sate 00-23.
# Ako je False, čita od 00 do trenutnog sata.
SINKRONIZIRAJ_SVA_24_SATA = True

# Ako EYWA schema nema relacijsko input polje `stanica` na DhmzMjerenjaInput,
# skripta će sama napraviti fallback bez relacije.
POKUSAJ_SINKRONIZIRATI_RELACIJU = True


# NAZIV: POMOĆNE FUNKCIJE
# OBJAŠNJENJE:
# Ovdje su funkcije koje nisu vezane samo za EYWA ili Selenium.
# One služe za logiranje, izradu DHMZ URL-a, odabir sati, parsiranje brojeva
# i čišćenje naziva stanica prije spremanja podataka.



# NAZIV: ZAPIS LOGA
# OBJAŠNJENJE: Ispisuje poruku s trenutnim datumom i vremenom u vremenskoj zoni Europe/Zagreb.
def zapisi_log(poruka: str) -> None:
    print(f"[{datetime.now(ZAGREB_VREMENSKA_ZONA).isoformat(timespec='seconds')}] {poruka}")



# NAZIV: IZGRADNJA DHMZ URL-A
# OBJAŠNJENJE: Prima sat od 0 do 23 i vraća ispravan DHMZ URL za taj sat. Ključ "param" ostaje nepreveden jer ga DHMZ očekuje u URL-u.
def izgradi_dhmz_url(sat: int) -> str:
    if not 0 <= sat <= 23:
        raise ValueError(f"Sat mora biti između 0 i 23: {sat}")

    upit = urlencode(
        {
            "section": DHMZ_SEKCIJA,
            # Ključ "param" ne prevodimo jer ga DHMZ URL očekuje baš tako.
            "param": DHMZ_PARAMETAR,
            "sat": f"{sat:02d}",
        }
    )
    return f"{DHMZ_OSNOVNI_URL}?{upit}"



# NAZIV: ODABIR SATI ZA SINKRONIZACIJU
# OBJAŠNJENJE: Vraća listu sati koje treba dohvatiti. Ako je uključeno svih 24 sata, vraća 0-23; inače vraća od 0 do trenutnog sata.
def dohvati_sate_za_sinkronizaciju(
    sada: datetime | None = None,
    sva_24_sata: bool = SINKRONIZIRAJ_SVA_24_SATA,
) -> list[int]:
    sada = sada or datetime.now(ZAGREB_VREMENSKA_ZONA)

    if sva_24_sata:
        return list(range(24))

    return list(range(sada.hour + 1))



# NAZIV: PARSIRANJE BROJEVA
# OBJAŠNJENJE: Pretvara vrijednosti iz DHMZ tablice u float. Prazne vrijednosti, crtice i neispravne brojeve pretvara u None.
def parsiraj_broj(vrijednost: str | None) -> float | None:
    """Pretvara DHMZ tekstualne brojeve u float; '-', prazno i slično pretvara u None."""
    if vrijednost is None:
        return None

    vrijednost = vrijednost.strip().replace(",", ".").replace("*", "")

    if vrijednost in ("", "-", "–"):
        return None

    try:
        return float(vrijednost)
    except ValueError:
        return None



# NAZIV: PROVJERA AUTOMATSKE STANICE
# OBJAŠNJENJE: Provjerava ima li naziv stanice oznaku A ili ^A, što označava automatska mjerenja.
def je_automatska_stanica(sirovi_naziv: str) -> bool:
    """DHMZ automatske postaje u nazivu obično imaju oznaku A ili ^A."""
    naziv = sirovi_naziv.strip()
    return bool(re.search(r"(?:\^A|\s+A\s*$)", naziv))



# NAZIV: ČIŠĆENJE NAZIVA STANICE
# OBJAŠNJENJE: Uklanja oznake A i ^A iz naziva stanice kako ista lokacija ne bi završila kao više različitih zapisa.
def ocisti_naziv_stanice(sirovi_naziv: str) -> str:
    """Čisti naziv postaje. Oznake A / ^A se uklanjaju iz naziva."""
    naziv = sirovi_naziv.strip()
    naziv = naziv.replace("^A", "")
    naziv = re.sub(r"\s+A$", "", naziv)
    naziv = re.sub(r"\s+", " ", naziv)
    return naziv.strip()


# NAZIV: EYWA GRAPHQL FUNKCIJE
# OBJAŠNJENJE:
# Ove funkcije šalju GraphQL upite i mutacije prema EYWA platformi.
# Njihov posao je pronaći postojeću stanicu ili mjerenje te spremiti novi
# zapis ili ažurirati postojeći zapis ako već postoji.



# NAZIV: SLANJE GRAPHQL ZAHTJEVA
# OBJAŠNJENJE: Centralna funkcija za slanje GraphQL upita prema EYWA platformi.
async def posalji_graphql(upit: str, varijable: dict | None = None):
    """Šalje GraphQL request prema EYWA koristeći eywa.graphql."""
    varijable = varijable or {}
    return await eywa.graphql(upit, varijable)



# NAZIV: PRONALAZAK POSTOJEĆE STANICE
# OBJAŠNJENJE: Traži stanicu u EYWA tablici DhmzStanica po polju lokacija.
async def pronadi_postojecu_stanicu(lokacija: str):
    upit = """
    query FindStation($lokacija: String!) {
      searchDhmzStanica(
        _where: {
          lokacija: {_eq: $lokacija}
        }
      ) {
        euuid
        lokacija
        automatska_mjerenja
        zadnje_pogledano
      }
    }
    """
    podaci = await posalji_graphql(upit, {"lokacija": lokacija})
    rezultati = podaci.get("searchDhmzStanica", [])
    return rezultati[0] if rezultati else None



# NAZIV: SPREMANJE ILI AŽURIRANJE STANICE
# OBJAŠNJENJE: Šalje stanicu u EYWA. Ako postoji euuid, EYWA ažurira zapis; ako ne postoji, kreira novi zapis.
async def sinkroniziraj_stanicu(stanica: dict, postojeci_euuid: str | None = None):
    mutacija = """
    mutation SyncStation($var: DhmzStanicaInput!) {
      syncDhmzStanica(data: $var) {
        euuid
        lokacija
        automatska_mjerenja
        zadnje_pogledano
      }
    }
    """

    podaci_za_slanje = {
        "lokacija": stanica["lokacija"],
        "automatska_mjerenja": stanica["automatska_mjerenja"],
        "zadnje_pogledano": stanica["zadnje_pogledano"],
    }

    if postojeci_euuid:
        podaci_za_slanje["euuid"] = postojeci_euuid

    podaci = await posalji_graphql(mutacija, {"var": podaci_za_slanje})
    return podaci["syncDhmzStanica"]



# NAZIV: PRONALAZAK POSTOJEĆEG MJERENJA
# OBJAŠNJENJE: Traži mjerenje po kombinaciji postaja + date_time kako se isti sat ne bi spremio više puta.
async def pronadi_postojece_mjerenje(postaja: str, datum_vrijeme: str):
    upit = """
    query FindMeasurement($postaja: String!, $dateTime: Timestamp!) {
      searchDhmzMjerenja(
        _where: {
          postaja: {_eq: $postaja}
          date_time: {_eq: $dateTime}
        }
      ) {
        euuid
        postaja
        date_time
      }
    }
    """
    podaci = await posalji_graphql(upit, {"postaja": postaja, "dateTime": datum_vrijeme})
    rezultati = podaci.get("searchDhmzMjerenja", [])
    return rezultati[0] if rezultati else None



# NAZIV: SPREMANJE ILI AŽURIRANJE MJERENJA
# OBJAŠNJENJE: Šalje mjerenje u EYWA i po potrebi ga povezuje s pripadajućom stanicom preko euuid-a.
async def sinkroniziraj_mjerenje(
    mjerenje: dict,
    postojeci_euuid: str | None = None,
    stanica_euuid: str | None = None,
    ukljuci_relaciju: bool = True,
):
    mutacija = """
    mutation SyncMeasurement($var: DhmzMjerenjaInput!) {
      syncDhmzMjerenja(data: $var) {
        euuid
        postaja
        date_time
        temperatura_zraka
        tlak_zraka
        vjetar_brzina
      }
    }
    """

    podaci_za_slanje = {
        "postaja": mjerenje["postaja"],
        "date_time": mjerenje["date_time"],
        "vjetar_smjer": mjerenje["vjetar_smjer"],
        "vjetar_brzina": mjerenje["vjetar_brzina"],
        "temperatura_zraka": mjerenje["temperatura_zraka"],
        "relativna_vlaznost": mjerenje["relativna_vlaznost"],
        "tlak_zraka": mjerenje["tlak_zraka"],
        "tendencija_tlaka": mjerenje["tendencija_tlaka"],
        "stanje_vremena": mjerenje["stanje_vremena"],
    }

    if postojeci_euuid:
        podaci_za_slanje["euuid"] = postojeci_euuid

    if ukljuci_relaciju and stanica_euuid:
        podaci_za_slanje["stanica"] = {"euuid": stanica_euuid}

    podaci = await posalji_graphql(mutacija, {"var": podaci_za_slanje})
    return podaci["syncDhmzMjerenja"]


# NAZIV: SELENIUM / DHMZ SCRAPER FUNKCIJE
# OBJAŠNJENJE:
# Ove funkcije otvaraju DHMZ stranicu u Chrome pregledniku, pronalaze glavnu
# tablicu s mjerenjima, čitaju retke tablice i pretvaraju ih u strukturirane
# Python dict zapise koje kasnije šaljemo u EYWA.


# NAZIV: POSTAVLJANJE SELENIUM PREGLEDNIKA
# OBJAŠNJENJE: Pokreće Chrome preglednik s opcijama za rad bez grafičkog sučelja na serveru.
def postavi_preglednik(bez_sucelja: bool = True):
    chrome_opcije = Options()

    if bez_sucelja:
        chrome_opcije.add_argument("--headless=new")
    else:
        chrome_opcije.add_argument("--start-maximized")

    chrome_opcije.add_argument("--no-sandbox")
    chrome_opcije.add_argument("--disable-dev-shm-usage")
    chrome_opcije.add_argument("--disable-gpu")
    chrome_opcije.add_argument("--window-size=1920,1080")

    return webdriver.Chrome(options=chrome_opcije)



# NAZIV: PRONALAZAK GLAVNE DHMZ TABLICE
# OBJAŠNJENJE: Čeka i pronalazi tablicu koja sadrži stupce Postaja, Temperatura, Relativna vlaga, Tlak i Stanje vremena.
def pronadi_glavnu_tablicu_mjerenja(preglednik):
    cekanje = WebDriverWait(preglednik, 20)
    return cekanje.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                """
                //table[
                    .//th[contains(normalize-space(.), 'Postaja')]
                    and .//th[contains(normalize-space(.), 'Temperatura')]
                    and .//th[contains(normalize-space(.), 'Relativna')]
                    and .//th[contains(normalize-space(.), 'Tlak')]
                    and .//th[contains(normalize-space(.), 'Stanje vremena')]
                ]
                """,
            )
        )
    )



# NAZIV: PARSIRANJE VREMENA MJERENJA
# OBJAŠNJENJE: Iz teksta DHMZ stranice izvlači stvarni datum i sat mjerenja. Ako ne uspije, koristi zamjenski sat.
def parsiraj_vrijeme_mjerenja_sa_stranice(preglednik, zamjenski_sat: int) -> str:
    """
    Izvlači vrijeme mjerenja iz naslova stranice.

    Primjer:
    "Vrijeme u Hrvatskoj 27.05.2026. u 14 h"
    -> 2026-05-27T14:00:00+02:00
    """
    tekst_stranice = preglednik.find_element(By.TAG_NAME, "body").text

    pronadeno = re.search(
        r"Vrijeme u Hrvatskoj\s+(\d{1,2})\.(\d{1,2})\.(\d{4})\.\s+u\s+(\d{1,2})\s*h",
        tekst_stranice,
        flags=re.IGNORECASE,
    )

    if pronadeno:
        dan, mjesec, godina, sat = map(int, pronadeno.groups())
        vrijeme_mjerenja = datetime(
            year=godina,
            month=mjesec,
            day=dan,
            hour=sat,
            minute=0,
            second=0,
            tzinfo=ZAGREB_VREMENSKA_ZONA,
        )
        return vrijeme_mjerenja.isoformat()

    sada = datetime.now(ZAGREB_VREMENSKA_ZONA)
    zamjensko_vrijeme = sada.replace(hour=zamjenski_sat, minute=0, second=0, microsecond=0)
    return zamjensko_vrijeme.isoformat()



# NAZIV: DOHVAT MJERENJA ZA JEDAN SAT
# OBJAŠNJENJE: Otvara DHMZ URL za jedan sat, čita tablicu i vraća listu dict zapisa sa stanicom i mjerenjem.
def scrape_dhmz_sat(preglednik, sat: int) -> list[dict]:
    url = izgradi_dhmz_url(sat)
    zapisi_log(f"Otvaram DHMZ sat {sat:02d}: {url}")
    preglednik.get(url)

    try:
        tablica = pronadi_glavnu_tablicu_mjerenja(preglednik)
    except TimeoutException:
        zapisi_log(f"Nema glavne tablice za sat {sat:02d}; preskačem.")
        return []

    datum_vrijeme = parsiraj_vrijeme_mjerenja_sa_stranice(preglednik, zamjenski_sat=sat)
    redovi = tablica.find_elements(By.XPATH, ".//tr[count(td)=8]")
    mjerenja = []

    for red in redovi:
        celije = red.find_elements(By.XPATH, "./td")
        if len(celije) != 8:
            continue

        vrijednosti = [celija.text.strip() for celija in celije]
        sirovi_naziv_stanice = vrijednosti[0]
        naziv_stanice = ocisti_naziv_stanice(sirovi_naziv_stanice)

        if not naziv_stanice:
            continue

        mjerenja.append(
            {
                "sat": sat,
                "stanica": {
                    "lokacija": naziv_stanice,
                    "automatska_mjerenja": je_automatska_stanica(sirovi_naziv_stanice),
                    "zadnje_pogledano": datum_vrijeme,
                },
                "mjerenje": {
                    "postaja": naziv_stanice,
                    "date_time": datum_vrijeme,
                    "vjetar_smjer": vrijednosti[1] or None,
                    "vjetar_brzina": parsiraj_broj(vrijednosti[2]),
                    "temperatura_zraka": parsiraj_broj(vrijednosti[3]),
                    "relativna_vlaznost": parsiraj_broj(vrijednosti[4]),
                    "tlak_zraka": parsiraj_broj(vrijednosti[5]),
                    "tendencija_tlaka": parsiraj_broj(vrijednosti[6]),
                    "stanje_vremena": vrijednosti[7] or None,
                },
            }
        )

    zapisi_log(f"Sat {sat:02d}: dohvatilo se {len(mjerenja)} redaka za date_time={datum_vrijeme}")
    return mjerenja



# NAZIV: DOHVAT DNEVNIH MJERENJA
# OBJAŠNJENJE: Prolazi kroz sve odabrane sate, dohvaća mjerenja i lokalno uklanja duplikate po postaja + date_time.
def scrape_dhmz_dnevna_mjerenja(
    bez_sucelja: bool = True,
    sva_24_sata: bool = SINKRONIZIRAJ_SVA_24_SATA,
) -> list[dict]:
    sati = dohvati_sate_za_sinkronizaciju(sva_24_sata=sva_24_sata)
    svi_redovi: list[dict] = []
    videni_zapisi: set[tuple[str, str]] = set()

    preglednik = postavi_preglednik(bez_sucelja=bez_sucelja)
    try:
        for sat in sati:
            redovi_za_sat = scrape_dhmz_sat(preglednik, sat)
            for red in redovi_za_sat:
                kljuc = (red["mjerenje"]["postaja"], red["mjerenje"]["date_time"])
                if kljuc in videni_zapisi:
                    continue
                videni_zapisi.add(kljuc)
                svi_redovi.append(red)
    finally:
        preglednik.quit()

    return svi_redovi


# NAZIV: LOGIKA SINKRONIZACIJE
# OBJAŠNJENJE:
# Ove funkcije povezuju scraper i EYWA dio.
# Prvo se provjerava postoji li stanica ili mjerenje, a zatim se radi upsert.
# U glavnoj funkciji sinkroniziraj_mjerenja_na_eywa koristi se dict cache
# za stanice po lokaciji, tako da se ista stanica ne ažurira za svaki sat.


# NAZIV: UPSERT JEDNE STANICE NA EYWA
# OBJAŠNJENJE: Provjerava postoji li stanica i zatim ju kreira ili ažurira. Ovu funkciju cache poziva samo jednom po lokaciji u jednom runu.
async def sinkroniziraj_stanicu_na_eywa(stanica: dict):
    postojeca_stanica = await pronadi_postojecu_stanicu(stanica["lokacija"])

    if postojeca_stanica:
        sinkronizirana_stanica = await sinkroniziraj_stanicu(
            stanica,
            postojeci_euuid=postojeca_stanica["euuid"],
        )
        return sinkronizirana_stanica, "azurirano"

    sinkronizirana_stanica = await sinkroniziraj_stanicu(stanica)
    return sinkronizirana_stanica, "kreirano"



# NAZIV: UPSERT JEDNOG MJERENJA NA EYWA
# OBJAŠNJENJE: Provjerava postoji li mjerenje za postaju i vrijeme, zatim ga kreira ili ažurira.
async def sinkroniziraj_mjerenje_na_eywa(mjerenje: dict, stanica_euuid: str | None = None):
    postojece_mjerenje = await pronadi_postojece_mjerenje(
        postaja=mjerenje["postaja"],
        datum_vrijeme=mjerenje["date_time"],
    )
    postojeci_euuid = postojece_mjerenje["euuid"] if postojece_mjerenje else None

    try:
        sinkronizirano_mjerenje = await sinkroniziraj_mjerenje(
            mjerenje=mjerenje,
            postojeci_euuid=postojeci_euuid,
            stanica_euuid=stanica_euuid,
            ukljuci_relaciju=POKUSAJ_SINKRONIZIRATI_RELACIJU,
        )
    except Exception as greska:
        if POKUSAJ_SINKRONIZIRATI_RELACIJU and stanica_euuid:
            zapisi_log(
                "EYWA nije prihvatila relacijsko polje `stanica` na DhmzMjerenjaInput; "
                f"ponavljam bez relacije. Detalj: {greska}"
            )
            sinkronizirano_mjerenje = await sinkroniziraj_mjerenje(
                mjerenje=mjerenje,
                postojeci_euuid=postojeci_euuid,
                stanica_euuid=None,
                ukljuci_relaciju=False,
            )
        else:
            raise

    return sinkronizirano_mjerenje, "azurirano" if postojece_mjerenje else "kreirano"



# NAZIV: GLAVNA SINKRONIZACIJA S CACHEOM STANICA
# OBJAŠNJENJE: Sinkronizira sve retke. Koristi dict cache_stanica gdje je ključ lokacija, a vrijednost EYWA objekt stanice, da se ista stanica ne ažurira više puta tijekom istog pokretanja.
async def sinkroniziraj_mjerenja_na_eywa(redovi: list[dict]) -> dict:
    stanice_kreirane = 0
    stanice_azurirane = 0
    stanice_iz_cachea = 0

    mjerenja_kreirana = 0
    mjerenja_azurirana = 0

    po_satu: dict[str, int] = {}

    # DICT CACHE ZA STANICE I LOKACIJE
    #
    # Problem bez cachea:
    # Ako se ista stanica pojavi u 24 različita sata, program bi 24 puta radio:
    # 1) GraphQL query za pronalazak stanice
    # 2) GraphQL mutation za ažuriranje stanice
    #
    # Rješenje:
    # Koristimo dict gdje je:
    # - ključ: lokacija stanice, npr. "Zagreb-Grič"
    # - vrijednost: EYWA objekt sinkronizirane stanice koji sadrži euuid
    #
    # Tako se svaka stanica u jednom pokretanju sinkronizira samo prvi put.
    # Sljedeća mjerenja za istu lokaciju samo koriste euuid iz cachea.
    cache_stanica: dict[str, dict] = {}

    for red in redovi:
        stanica = red["stanica"]
        mjerenje = red["mjerenje"]

        oznaka_sata = mjerenje["date_time"][11:13]
        po_satu[oznaka_sata] = po_satu.get(oznaka_sata, 0) + 1

        naziv_stanice = stanica["lokacija"]

        # Ako je ova lokacija već obrađena u ovom pokretanju,
        # ne šaljemo ponovno stanicu u EYWA nego uzimamo spremljeni euuid iz dict cachea.
        if naziv_stanice in cache_stanica:
            sinkronizirana_stanica = cache_stanica[naziv_stanice]
            stanice_iz_cachea += 1
        else:
            # Ako lokacija nije u cacheu, ovo je prvi put da vidimo tu stanicu u ovom runu.
            # Tada radimo normalan upsert prema EYWA i rezultat spremamo u cache.
            sinkronizirana_stanica, akcija_stanice = await sinkroniziraj_stanicu_na_eywa(stanica)

            if akcija_stanice == "kreirano":
                stanice_kreirane += 1
            else:
                stanice_azurirane += 1

            cache_stanica[naziv_stanice] = sinkronizirana_stanica

        sinkronizirano_mjerenje, akcija_mjerenja = await sinkroniziraj_mjerenje_na_eywa(
            mjerenje=mjerenje,
            stanica_euuid=sinkronizirana_stanica.get("euuid"),
        )

        if akcija_mjerenja == "kreirano":
            mjerenja_kreirana += 1
            zapisi_log(
                f"Kreirano mjerenje: {sinkronizirano_mjerenje['postaja']} / "
                f"{sinkronizirano_mjerenje['date_time']}"
            )
        else:
            mjerenja_azurirana += 1
            zapisi_log(
                f"Updateano mjerenje: {sinkronizirano_mjerenje['postaja']} / "
                f"{sinkronizirano_mjerenje['date_time']}"
            )

    return {
        "stanice_kreirane": stanice_kreirane,
        "stanice_azurirane": stanice_azurirane,
        "stanice_iz_cachea": stanice_iz_cachea,
        "mjerenja_kreirana": mjerenja_kreirana,
        "mjerenja_azurirana": mjerenja_azurirana,
        "ukupno": len(redovi),
        "po_satu": po_satu,
        "jedinstvene_stanice": len(cache_stanica),
    }


# NAZIV: MAIN / GLAVNO POKRETANJE PROGRAMA
# OBJAŠNJENJE:
# Ovdje se nalazi glavni tok programa.
# Funkcija run_once pokreće jedan dnevni sync, a run_every_day je opcionalni
# interni scheduler ako se program ne pokreće preko EYWA schedulera.


# NAZIV: JEDNO POKRETANJE PROGRAMA
# OBJAŠNJENJE: Pokreće EYWA task, dohvaća DHMZ podatke, sinkronizira ih i šalje završni izvještaj.
async def run_once(
    headless: bool = True,
    all_24_hours: bool = SINKRONIZIRAJ_SVA_24_SATA,
):
    """
    Pokreni jedan dnevni sync.

    Parametri su ostali na engleskom radi kompatibilnosti s postojećim pozivima:
    run_once(headless=True, all_24_hours=True).
    Unutar funkcije se koriste hrvatski nazivi.
    """
    bez_sucelja = headless
    sva_24_sata = all_24_hours

    eywa.open_pipe()

    try:
        zadatak = await eywa.get_task()
        eywa.info(f"Task primljen: {zadatak}")
        eywa.update_task(eywa.PROCESSING)

        redovi = scrape_dhmz_dnevna_mjerenja(
            bez_sucelja=bez_sucelja,
            sva_24_sata=sva_24_sata,
        )
        zapisi_log(f"Ukupno dohvaćeno redaka s DHMZ-a: {len(redovi)}")

        if not redovi:
            zapisi_log("Nema podataka za sync.")
            eywa.close_task(eywa.SUCCESS)
            return

        rezultat = await sinkroniziraj_mjerenja_na_eywa(redovi)

        poruka = (
            "DHMZ dnevni sync gotov. "
            f"Stanice kreirano: {rezultat['stanice_kreirane']}, "
            f"stanice ažurirano: {rezultat['stanice_azurirane']}, "
            f"stanice iz cachea: {rezultat['stanice_iz_cachea']}, "
            f"jedinstvenih stanica: {rezultat['jedinstvene_stanice']}, "
            f"mjerenja kreirano: {rezultat['mjerenja_kreirana']}, "
            f"mjerenja ažurirano: {rezultat['mjerenja_azurirana']}, "
            f"ukupno redaka: {rezultat['ukupno']}, "
            f"po satu: {rezultat['po_satu']}"
        )

        zapisi_log(poruka)
        eywa.report(poruka, rezultat)
        eywa.close_task(eywa.SUCCESS)

    except Exception as greska:
        eywa.error(f"Greška: {str(greska)}")
        eywa.close_task(eywa.ERROR)
        raise



# NAZIV: OPCIONALNI DNEVNI SCHEDULER
# OBJAŠNJENJE: Drži proces aktivnim i jednom dnevno poziva run_once ako se ne koristi vanjski scheduler.
async def run_every_day(hour: int = 23, minute: int = 30, headless: bool = True):
    """
    Opcionalni interni scheduler.

    Parametri su ostali na engleskom radi kompatibilnosti s postojećim pozivima.
    Ako EYWA platforma već ima daily schedule, koristi samo run_once().
    """
    sat = hour
    minuta = minute
    bez_sucelja = headless

    while True:
        sada = datetime.now(ZAGREB_VREMENSKA_ZONA)
        sljedece_pokretanje = sada.replace(hour=sat, minute=minuta, second=0, microsecond=0)

        if sljedece_pokretanje <= sada:
            sljedece_pokretanje += timedelta(days=1)

        sekunde_do_pokretanja = (sljedece_pokretanje - sada).total_seconds()
        zapisi_log(f"Sljedeći dnevni sync: {sljedece_pokretanje.isoformat(timespec='minutes')}")
        await asyncio.sleep(sekunde_do_pokretanja)

        try:
            await run_once(headless=bez_sucelja, all_24_hours=True)
        except Exception as greska:
            zapisi_log(f"Greška u dnevnom syncu: {greska}")


if __name__ == "__main__":
    asyncio.run(run_once(headless=True, all_24_hours=True))

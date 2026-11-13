"""
Telegram bot koji prati Gmail i šalje AI sažetak (Gemini):
  - svako jutro u 07:00  (sve što je stiglo od poslednjeg sažetka)
  - tokom dana (07-23h)  čim stigne 4 nova mejla
  - uveče u 23:00

Pokretanje:
  python3 mejl_bot.py              # pokreće bota (ostavi da radi)
  python3 mejl_bot.py --chat-id    # ispiše tvoj Telegram chat id
  python3 mejl_bot.py --test       # prikaže poslednja 4 mejla (bez slanja)
  python3 mejl_bot.py --once       # jedna provera pa kraj (za server/cron)
  python3 mejl_bot.py --now        # odmah pošalje sažetak novih mejlova
"""
import email
import imaplib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import decode_header, make_header
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"

# ----------------------------------------------------------------- podešavanja
def ucitaj_env():
    p = BASE / ".env"
    if p.exists():
        for linija in p.read_text().splitlines():
            linija = linija.strip()
            if linija and not linija.startswith("#") and "=" in linija:
                k, v = linija.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


ucitaj_env()
EMAIL = os.environ.get("EMAIL", "")
PASSWORD = os.environ.get("PASSWORD", "").replace(" ", "")
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELI = [m.strip() for m in (os.environ.get("GEMINI_MODELI") or
                 "gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.5-flash").split(",") if m.strip()]

PRAG_MEJLOVA = int(os.environ.get("PRAG_MEJLOVA") or 4)          # koliko novih mejlova okida sažetak tokom dana
DNEVNI_POCETAK = 7        # sati: od kad važi pravilo "4 mejla"
RASPORED = [(7, 0), (23, 0)]   # fiksni sažeci (sat, minut)
KASNJENJE_MIN = 60        # ako je računar spavao, nadoknadi sažetak do 60 min kasnije
PROVERA_SEK = int(os.environ.get("PROVERA_MIN") or 2) * 60         # na koliko sekundi proverava inbox
MAX_U_SAZETKU = 30        # najviše mejlova po jednom sažetku
STIL = os.environ.get("STIL", "").strip()
TZ = ZoneInfo(os.environ.get("TZ_BOT", "Europe/Belgrade"))
MAX_TELA = 1500           # najviše znakova tela po mejlu koje šaljemo Gemini-ju


# ----------------------------------------------------------------------- stanje
def ucitaj_stanje():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def sacuvaj_stanje(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


# ------------------------------------------------------------------------ Gmail
def imap_konekcija():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL, PASSWORD)
    mail.select("inbox", readonly=True)  # readonly: ne označava mejlove kao pročitane
    return mail


def dekodiraj(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def izvuci_telo(poruka):
    if poruka.is_multipart():
        for deo in poruka.walk():
            if deo.get_content_type() == "text/plain" and not deo.get_filename():
                return dekodiraj_deo(deo)
        for deo in poruka.walk():  # fallback: HTML bez tagova
            if deo.get_content_type() == "text/html":
                return ukloni_html(dekodiraj_deo(deo))
        return ""
    tekst = dekodiraj_deo(poruka)
    return ukloni_html(tekst) if poruka.get_content_type() == "text/html" else tekst


def dekodiraj_deo(deo):
    payload = deo.get_payload(decode=True)
    if payload is None:
        return ""
    return payload.decode(deo.get_content_charset() or "utf-8", errors="replace")


def ukloni_html(h):
    import re
    h = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def najveci_uid(mail):
    status, data = mail.uid("search", None, "ALL")
    uids = data[0].split() if status == "OK" else []
    return int(uids[-1]) if uids else 0


def novi_uidovi(mail, posle_uid):
    """UID-ovi svih mejlova u inboxu novijih od posle_uid."""
    status, data = mail.uid("search", None, f"UID {posle_uid + 1}:*")
    if status != "OK":
        return []
    # Gmail uvek vrati poslednji mejl za opseg "N:*", pa filtriramo
    return [int(u) for u in data[0].split() if int(u) > posle_uid]


def dohvati_mejl(mail, uid):
    status, data = mail.uid("fetch", str(uid), "(BODY.PEEK[])")
    if status != "OK" or not data or not data[0]:
        return None
    poruka = email.message_from_bytes(data[0][1])
    return {
        "uid": uid,
        "od": dekodiraj(poruka.get("From")),
        "naslov": dekodiraj(poruka.get("Subject")) or "(bez naslova)",
        "datum": poruka.get("Date", ""),
        "telo": izvuci_telo(poruka)[:MAX_TELA],
    }


# ----------------------------------------------------------------------- Gemini
def gemini_sazetak(mejlovi, razlog):
    tekst = ""
    for i, m in enumerate(mejlovi, 1):
        tekst += f"--- Mejl {i} ---\nOd: {m['od']}\nNaslov: {m['naslov']}\nTekst: {m['telo']}\n\n"
    prompt = (
        "Ti si Fred, moj najbolji drug i lični asistent za mejlove. Pišeš na srpskom, opušteno, razdragano, "
        "onako bratski i uz zafrkanciju. Oslovljavaj me na razne načine: brt, brate, buraz, burazengijo, "
        "brozila, šefe, legendo i slično. VAŽNO: svaki put drugačije, menjaj uvod, oslovljavanje i način "
        "na koji saopštavaš stvari, ali uvek u tom bratskom fazonu (ne ponavljaj iste fraze).\n\n"
        f"Povod poruke: {razlog}.\n\n"
        "Kako da izgleda poruka:\n"
        "1. Kratak bratski uvod i koliko je novih mejlova stiglo.\n"
        "2. Za SVAKI mejl sam proceni šta je: spam, newsletter, automatska obaveštenja ili nešto stvarno. "
        "Nebitne stvari (spam, newsletteri tipa TLDR, AI Breakfast, promo, Google security alerti i sl.) "
        "grupiši i objasni u jednoj-dve rečenice, uz komentar da li vredi čitati (npr. 'uglavnom smor, "
        "ali ako ti se čita, tu je'; ako je u njemu nešto bitno, reci šta).\n"
        "3. Ako mejl deluje kao da ga je poslala prava osoba ili nešto što može biti važno (posao, "
        "prijave, faktura, dogovor, lična poruka), izdvoji ga posebno, npr. 'e ali vidi ovo, stiglo je "
        "nešto novo od <ime>' i objasni šta tačno piše i da li traži moj odgovor. Prvo proceni da li je "
        "možda spam pa tek onda reci da je važno.\n"
        "4. Na kraju kratak bratski zaključak: ako nema ničeg bitnog, reci nešto u fazonu 'lagan si, "
        "ništa spec ovaj put'; ako ima, reci šta da odradim prvo.\n\n"
        "Bez markdown formatiranja (bez zvezdica i #), samo običan tekst, sa pokojim emotikonom. "
        "Neka poruka bude kratka i pregledna, ne piši roman. "
        + (f"Dodatne želje u stilu: {STIL}\n\n" if STIL else "\n\n") + tekst
    )
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    poslednja = None
    for model in GEMINI_MODELI:  # ako jedan model ne radi/sporo je, probamo sledeći
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for pokusaj in range(2):
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    out = json.load(r)
                tekst = out["candidates"][0]["content"]["parts"][0]["text"].strip()
                if tekst:
                    print(f"Gemini: {model} OK")
                    return tekst
                poslednja = f"{model}: prazan odgovor"
            except urllib.error.HTTPError as e:
                poslednja = f"{model}: HTTP {e.code}"
                if e.code not in (429, 500, 503):
                    break  # npr. 404 (model ne postoji) -> odmah sledeći model
            except Exception as e:  # timeout, mrežna greška, neočekivan odgovor
                poslednja = f"{model}: {type(e).__name__}: {e}"
            print(f"Gemini problem ({poslednja}), pokušaj {pokusaj + 1}")
            time.sleep(5)
    raise RuntimeError(f"Nijedan Gemini model nije odgovorio ({poslednja})")


def osnovni_sazetak(mejlovi):
    """Rezervna varijanta ako Gemini nije dostupan: samo pošiljaoci i naslovi."""
    return "\n".join(f"• {m['od']}\n  {m['naslov']}" for m in mejlovi)


# --------------------------------------------------------------------- Telegram
def telegram_api(metoda, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{metoda}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        return json.load(r)


def posalji_telegram(tekst):
    for i in range(0, len(tekst), 4000):  # limit poruke je 4096 znakova
        r = telegram_api("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=tekst[i:i + 4000])
        print(f"Telegram: poslato (message_id={r['result']['message_id']}, chat={r['result']['chat']['id']})")


# ------------------------------------------------------------------ glavna logika
def posalji_sazetak(razlog, stanje, bez_slanja=False):
    """Pošalje sažetak svih mejlova stiglih posle stanje['last_uid']."""
    mail = imap_konekcija()
    try:
        uidovi = novi_uidovi(mail, stanje["last_uid"])
        if not uidovi:
            return 0
        izabrani = uidovi[-MAX_U_SAZETKU:]
        mejlovi = [m for m in (dohvati_mejl(mail, u) for u in izabrani) if m]
    finally:
        mail.logout()

    try:
        sazetak = gemini_sazetak(mejlovi, razlog)
    except Exception as e:
        print(f"Gemini greška: {e} -> šaljem osnovni spisak")
        sazetak = osnovni_sazetak(mejlovi)

    zaglavlje = f"📬 {razlog} — {len(uidovi)} novih mejlova"
    if len(uidovi) > len(izabrani):
        zaglavlje += f" (prikazano poslednjih {len(izabrani)})"
    poruka = f"{zaglavlje}\n\n{sazetak}"

    if bez_slanja:
        print(poruka)
    else:
        posalji_telegram(poruka)
    stanje["last_uid"] = max(uidovi)
    sacuvaj_stanje(stanje)
    return len(uidovi)


def broj_novih(stanje):
    mail = imap_konekcija()
    try:
        return len(novi_uidovi(mail, stanje["last_uid"]))
    finally:
        mail.logout()


def pripremi_stanje():
    stanje = ucitaj_stanje()
    if "last_uid" not in stanje:
        # prvi start: ne obrađujemo staru poštu, računamo samo ono što stigne od sad
        mail = imap_konekcija()
        stanje["last_uid"] = najveci_uid(mail)
        mail.logout()
        print(f"Prvi start, počinjem od UID {stanje['last_uid']}.")
    stanje.setdefault("done", {})  # "datum HH:MM" -> True
    sacuvaj_stanje(stanje)
    return stanje


def jedan_krug(stanje):
    """Jedna provera: fiksni sažeci (07/23) ili prag novih mejlova tokom dana."""
    sada = datetime.now(TZ)
    uradjeno = False

    # 1) fiksni sažeci
    for sat, minut in RASPORED:
        kljuc = f"{sada:%Y-%m-%d} {sat:02d}:{minut:02d}"
        zakazano = sada.replace(hour=sat, minute=minut, second=0, microsecond=0)
        kasni = (sada - zakazano).total_seconds() / 60
        if 0 <= kasni <= KASNJENJE_MIN and kljuc not in stanje["done"]:
            n = posalji_sazetak(f"Redovan sažetak u {sat:02d}:{minut:02d}", stanje)
            if n == 0:
                posalji_telegram(f"📭 Nema novih mejlova ({sat:02d}:{minut:02d}), možeš da dišeš, brate.")
            stanje["done"] = {k: True for k in stanje["done"] if k.startswith(f"{sada:%Y-%m-%d}")}
            stanje["done"][kljuc] = True
            sacuvaj_stanje(stanje)
            uradjeno = True

    # 2) tokom dana: čim se skupi dovoljno novih mejlova
    if not uradjeno and DNEVNI_POCETAK <= sada.hour < 23:
        n = broj_novih(stanje)
        if n >= PRAG_MEJLOVA:
            posalji_sazetak(f"Stiglo je {n} novih mejlova", stanje)


def petlja(sati=None):
    stanje = pripremi_stanje()
    rok = time.time() + sati * 3600 if sati else None
    print("Bot radi." + (f" Radim još {sati} h pa završavam." if sati else " Ctrl+C za prekid."))
    while rok is None or time.time() < rok:
        try:
            jedan_krug(stanje)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{datetime.now(TZ):%H:%M:%S}] Greška: {e}")
        time.sleep(PROVERA_SEK)


# ---------------------------------------------------------------------- CLI
def proveri_podesavanja(*potrebno):
    fali = [p for p in potrebno if not globals().get(p)]
    if fali:
        sys.exit(f"Fali u .env: {', '.join(fali)}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    if arg == "--chat-id":
        proveri_podesavanja("TELEGRAM_TOKEN")
        res = telegram_api("getUpdates")["result"]
        if not res:
            sys.exit("Nema poruka. Otvori svog bota u Telegramu, pošalji mu /start pa pokreni ponovo.")
        for u in res:
            c = (u.get("message") or {}).get("chat")
            if c:
                print(f"chat id: {c['id']}  ({c.get('first_name', '')} {c.get('username', '')})")

    elif arg == "--test":
        proveri_podesavanja("EMAIL", "PASSWORD")
        mail = imap_konekcija()
        poslednji = najveci_uid(mail)
        print(f"Najveći UID u inboxu: {poslednji}\nPoslednja 4 mejla:\n")
        status, data = mail.uid("search", None, "ALL")
        for u in [int(x) for x in data[0].split()][-4:]:
            m = dohvati_mejl(mail, u)
            print(f"[{m['uid']}] Od: {m['od']}\n     Naslov: {m['naslov']}\n     Tekst: {m['telo'][:100]!r}\n")
        mail.logout()

    elif arg == "--sati":
        # radi N sati u petlji pa završi (GitHub Actions ga posle toga sam ponovo pokrene)
        proveri_podesavanja("EMAIL", "PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
        petlja(float(sys.argv[2]))

    elif arg == "--once":
        # jedna provera pa kraj (za server/cron/GitHub Actions)
        proveri_podesavanja("EMAIL", "PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
        jedan_krug(pripremi_stanje())

    elif arg == "--now":
        proveri_podesavanja("EMAIL", "PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
        st = ucitaj_stanje()
        if "last_uid" not in st:
            sys.exit("Prvo pokreni bota jednom (python3 mejl_bot.py) da zapamti početnu tačku.")
        print("Poslato mejlova:", posalji_sazetak("Ručni sažetak", st))

    else:
        proveri_podesavanja("EMAIL", "PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
        try:
            petlja()
        except KeyboardInterrupt:
            print("\nBot zaustavljen.")

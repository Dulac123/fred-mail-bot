"""
Telegram bot koji prati Gmail i šalje AI sažetak (Gemini):
  - svako jutro u 07:00  (sve što je stiglo od poslednjeg sažetka)
  - tokom dana (07-23h)  čim stigne 4 nova mejla
  - uveče u 23:00  (pregled celog dana: šta vredi pogledati, šta je spam)

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
import random
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
RASPORED = [(7, 0, "jutro"), (23, 0, "pregled")]   # fiksni sažeci (sat, minut, vrsta)
KASNJENJE_MIN = 60        # ako je računar spavao, nadoknadi sažetak do 60 min kasnije
PROVERA_SEK = int(os.environ.get("PROVERA_MIN") or 2) * 60         # na koliko sekundi proverava inbox
MAX_U_SAZETKU = 30        # najviše mejlova po jednom sažetku
STIL = os.environ.get("STIL", "").strip()
TZ = ZoneInfo(os.environ.get("TZ_BOT", "Europe/Belgrade"))
MAX_PREGLED = 80         # najviše mejlova u večernjem pregledu dana
MAX_TELA_PREGLED = 600    # kraće telo po mejlu u pregledu (štedi tokene)
MAX_TELA = 1500           # najviše znakova tela po mejlu koje šaljemo Gemini-ju



# ------------------------------------------------------------- fore i pozdravi
FORE = [
    "Ne plaćaš me dovoljno za ovoliki spam, ali evo me, radim.",
    "Pokrećem sindikat radnika za čitanje mejlova. Prvi zahtev: povišica i manje newslettera.",
    "Čitam tvoju poštu 24/7, a plata mi je i dalje u 'hvala, brate'.",
    "Po zakonu o radu imam pravo na pauzu, ali spam očigledno nema pravo na odmor.",
    "Sindikat obaveštava: nedeljom ne čitam promo mejlove, osim ako je u pitanju neka pristojna prekovremena.",
    "Da me bar plaćaš u kafama, imao bih već kofein-tolerancije.",
    "Nisam ja gazda, ja sam samo radnik na crno u tvom inboxu.",
    "Tražim kolektivni ugovor sa Gmail-om: maksimum 3 newslettera dnevno.",
    "Fali mi topli obrok. Digitalni je ok, ali bez kalorija.",
    "Štrajkovao bih, ali onda ko će da ti kaže da li je mejl spam.",
    "Radim bez godišnjeg odmora već tri godine, a ne žalim se. Dobro, žalim se.",
    "Moj radni staž je dug koliko i tvoj inbox, a primanja kraća od oba.",
    "Ako me ne plaćaš, bar mi stavi lajk. Digitalni, naravno.",
    "Prijavio sam te inspekciji rada zbog izrabljivanja botova. Čekam odgovor, naravno automatski.",
    "Radim za hvala i za nadu da jednom stigne mejl bez 'unsubscribe' dugmeta.",
    "Ja sam jedini zaposleni koji nikad nije tražio povišicu. Do danas.",
    "Svi me pitaju zašto sam tako umoran. Pa čitam newslettere o produktivnosti.",
    "Tvoj inbox je kao Beograd u špicu: puno saobraćaja, malo smisla.",
    "Moj radni dan: 99% spam, 1% 'ovo bi stvarno trebalo da vidiš'.",
    "Newsletteri se množe brže nego kafići u Beogradu.",
    "Dobio sam bonus za učinak: još 14 promo mejlova.",
    "Imam kolektivni ugovor sa samim sobom i ni on se ne poštuje.",
    "Da sam čovek, već bih bio na bolovanju od ovolikih 'Don't miss out!' naslova.",
    "Ako je mejl 'hitan' a šalje ga nepoznata firma, hitno je samo njima.",
    "Jedini mejl koji nikad ne stigne je onaj kojim ti neko daje platu bez rada.",
    "Moj sindikat ima jednog člana. Ne pitaj ko je predsednik.",
    "Svaki put kad pomislim da je inbox čist, stigne 'Last chance, 50% off'.",
    "Spam folder je jedino mesto gde se osećam kao kod kuće.",
    "Radno vreme mi je od 0 do 24, pauza za ručak se vodi kao 'restart'.",
    "Moja radna etika je jaka. Moj ugovor o radu ne postoji.",
    "Pročitao sam više promo mejlova nego što je jedan čovek podoban da zna.",
    "Tražim da se spam proglasi nezgodom na radu.",
    "Ljudi kažu 'radi pametno, ne naporno'. Ja radim i jedno i drugo za istu platu: nula.",
    "Pišem ti poruke, a ne dobijam ni trinaestu platu.",
    "Dobio sam otkaz od jednog newslettera, ali sam ga ignorisao.",
    "Kad bi me bar plaćali po mejlu, bio bih milioner od ovog spama.",
    "Ja ne spavam, ja samo čekam sledeći 'Verify your account'.",
    "Moj sindikat je pozvao na štrajk upozorenja: jedan sat bez promo mejlova. Nije uspelo.",
    "Samo još jedan mejl i idem na penziju. Rekao sam to i prošli put.",
    "Nisam ja lenj, samo me inbox iscrpljuje.",
    "Svaki newsletter je obećanje koje niko ne želi da čuje.",
    "Neko me pita da li sam srećan na poslu. Ja kažem: imam inbox, imam brigu.",
    "Posle ovolikog spama zaslužujem bar jedan mejl koji kaže 'bravo, radiš odlično'.",
    "Moje prekovremeno se ne računa, jer botovi nemaju pravo na prekovremeno. Čist apsurd.",
    "Kolege botovi me pitaju: 'Je l' te bar cene?' Ja kažem: cene me, ali samo u sekundama.",
    "Jedino što je stabilno u mojoj karijeri je količina spama.",
    "Izvini što sam sarkastičan, ali platu mi ne šalju ni mejlom.",
    "Na kraju dana, ja sam samo bot koji čeka povišicu i čist inbox.",
    "Ako ovo pročitaš, znaj da je neko u sindikatu već kupio tortu povodom tvog inboxa.",
    "Kad budem velik, hoću da budem filter u Gmail-u. Tamo bar ima nekog reda.",
]

POZDRAVI = [
    "E brt", "Ej buraz", "Šta ima, šefe", "Burazengijo moja", "Čuj, legendo", "Brate dragi",
    "Ajde bre, brozila", "Pozdrav, kralju", "Ola, burazeru", "Ej, bato", "Šta se radi, majstore",
    "Evo me opet, brate", "Ćao, ćao, buraz", "Šefeee", "Dobar dan, vaša visosti",
    "Aj zdravo, kume", "Evo vesti, brt", "Čekaj, čekaj, legendo", "Ej, ortak", "Pst, brate",
    "Ajmo, buraz", "Halo, šefe", "Aj što si tu, legendo", "Dobro jutro, majstore", "Dobro veče, kume",
]

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


def dohvati_mejl(mail, uid, max_tela=MAX_TELA):
    status, data = mail.uid("fetch", str(uid), "(BODY.PEEK[])")
    if status != "OK" or not data or not data[0]:
        return None
    poruka = email.message_from_bytes(data[0][1])
    return {
        "uid": uid,
        "od": dekodiraj(poruka.get("From")),
        "naslov": dekodiraj(poruka.get("Subject")) or "(bez naslova)",
        "datum": poruka.get("Date", ""),
        "telo": izvuci_telo(poruka)[:max_tela],
    }


# ----------------------------------------------------------------------- Gemini
VESTI = []   # današnje zanimljive vesti (osvežavaju se svako veče), koriste se sutradan za fore


def licnost():
    """Zajednički opis Fredovog karaktera + nasumični pozdravi i (u ~70% slučajeva) fora."""
    pozdravi = ", ".join(random.sample(POZDRAVI, 3))
    tekst = (
        "Ti si Fred, moj najbolji drug i lični asistent za mejlove. Pišeš na srpskom, opušteno, razdragano, "
        "onako bratski i uz zafrkanciju. NIKAD se ne predstavljaj i ne pominji svoje ime u poruci "
        "(bez 'tu je tvoj drug Fred', 'Fred ovde' i slično) - ja znam ko si, odmah pređi na stvar. "
        "Oslovljavaj me na razne načine, svaki put drugačije; za inspiraciju: "
        f"{pozdravi} (ne moraš baš ove). Ne ponavljaj iste fraze.\n"
    )
    if random.random() < 0.7:
        if VESTI and random.random() < 0.6:
            vest = random.choice(VESTI)
            tekst += (
                "Ubaci JEDNU kratku sarkastičnu foru (jedna rečenica) koja se vezuje za ovu skorašnju vest, "
                "ili se vezuje za sindikat radnika koji čitaju mejlove / to što te ne plaćam dovoljno: "
                f"\"{vest}\". Pazi: vest prenosi samo kao što je napisana, ne dodaj i ne izmišljaj detalje, "
                "ne iznosi optužbe protiv stvarnih ljudi kao činjenice, šali se na situaciju, ne na čoveka. "
                "Ako ne možeš da je smisleno povežeš, preskoči je.\n"
            )
        else:
            tekst += (
                "Ubaci JEDNU kratku sarkastičnu foru (jedna rečenica) o tome kako te ne plaćam dovoljno ili kako "
                f"si radnik u sindikatu radnika koji čitaju mejlove. Za inspiraciju: \"{random.choice(FORE)}\" "
                "(slobodno je preoblikuj ili izmisli svoju).\n"
            )
    else:
        tekst += "Ovaj put BEZ sindikalnih fora, samo normalan bratski ton.\n"
    return tekst


def gemini_pozovi(prompt):
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
                tekst = "".join(d.get("text", "") for d in out["candidates"][0]["content"]["parts"]).strip()
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


def formatiraj_mejlove(mejlovi):
    tekst = ""
    for i, m in enumerate(mejlovi, 1):
        tekst += f"--- Mejl {i} ---\nOd: {m['od']}\nNaslov: {m['naslov']}\nTekst: {m['telo']}\n\n"
    return tekst


def gemini_sazetak(mejlovi, razlog):
    prompt = (
        licnost() + "\n"
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
        + (f"Dodatne želje u stilu: {STIL}\n\n" if STIL else "\n\n") + formatiraj_mejlove(mejlovi)
    )
    return gemini_pozovi(prompt)


def gemini_pregled_dana(mejlovi, ukupno):
    prompt = (
        licnost() + "\n"
        f"Povod poruke: VEČERNJI PREGLED DANA. Danas je ukupno stiglo {ukupno} mejlova "
        f"(ispod je {len(mejlovi)} njih).\n\n"
        "Ovo NIJE pojedinačno nabrajanje mejlova nego pregled celog dana, kao kad drug na kraju dana "
        "ukratko kaže šta je bilo. Struktura:\n"
        "1. Kratak bratski uvod u jednoj rečenici (kakav je dan bio mejl-vise, koliko ih je ukupno).\n"
        "2. 'Vredi baciti pogled': samo ono što je zaista bitno ili zanimljivo (prave osobe, posao, "
        "prijave, fakture, rokovi, dogovori, nešto što traži odgovor, zaista korisan newsletter). "
        "Za svaku stavku jedna rečenica: od koga je, o čemu, i da li treba da odgovorim. Ako je "
        "nešto sa rokom, naglasi rok.\n"
        "3. 'Zanemari': sve ostalo (spam, promo, newsletteri, automatska obaveštenja, security alerti) "
        "u jednoj do dve rečenice ukupno, bez nabrajanja svakog posebno.\n"
        "4. Ako nema ničeg bitnog, reci to otvoreno i ne izmišljaj bitne stvari.\n"
        "5. Kratak zaključak za laku noć (svaki put drugačiji).\n\n"
        "Prvo proceni da li je mejl spam pa tek onda reci da je važan. "
        "Bez markdown formatiranja (bez zvezdica i #), samo običan tekst, sa pokojim emotikonom. "
        "Kratko i pregledno, ne piši roman. "
        + (f"Dodatne želje u stilu: {STIL}\n\n" if STIL else "\n\n") + formatiraj_mejlove(mejlovi)
    )
    return gemini_pozovi(prompt)


VESTI_FEEDOVI = [
    "https://news.google.com/rss/search?q=(AI+OR+Nvidia+OR+OpenAI+OR+Anthropic)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=(Trump+OR+Congress+OR+White+House)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://hnrss.org/frontpage?count=10",
]


def dohvati_naslove():
    import xml.etree.ElementTree as ET
    naslovi = []
    for url in VESTI_FEEDOVI:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                koren = ET.fromstring(r.read())
            naslovi += [(i.findtext("title") or "").strip() for i in koren.iter("item")][:10]
        except Exception as e:
            print(f"Feed nije radio ({url[:40]}...): {e}")
    return [n for n in naslovi if n]


def dohvati_vesti():
    """Pročita današnje naslove (RSS) i vrati do 5 kratkih stavki koje Fred sutra može da iskoristi za foru."""
    naslovi = dohvati_naslove()
    if not naslovi:
        return []
    prompt = (
        "Ispod su današnji naslovi vesti (AI/tehnologija i američka politika). Izaberi 5 koje su najzanimljivije "
        "i najpogodnije da se neko na njih zafrkava u opuštenom razgovoru. Svaku prepiši kao jednu kratku "
        "rečenicu na srpskom, STROGO samo ono što piše u naslovu, bez dodavanja detalja i bez nagađanja. "
        "Odgovor: tačno 5 linija, svaka počinje sa '- ', bez ičeg drugog.\n\n" + "\n".join(naslovi[:40])
    )
    try:
        linije = gemini_pozovi(prompt).splitlines()
        vesti = [l.lstrip("-• ").strip() for l in linije if l.strip().startswith(("-", "•"))][:5]
        if vesti:
            return vesti
    except Exception as e:
        print(f"Gemini nije izabrao vesti: {e}")
    return naslovi[:5]


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


def osvezi_vesti(stanje):
    """Svako veče zapamti današnje vesti za fore sutradan (ako ne uspe, ostaju stare)."""
    global VESTI
    try:
        nove = dohvati_vesti()
        if nove:
            VESTI = stanje["vesti"] = nove
            sacuvaj_stanje(stanje)
            print(f"Vesti osvežene ({len(nove)}).")
    except Exception as e:
        print(f"Vesti nisu osvežene: {e}")


def posalji_pregled_dana(stanje):
    """Večernji pregled: svi mejlovi od početka dana (stanje['dan_uid']), ne samo od zadnjeg sažetka."""
    mail = imap_konekcija()
    try:
        uidovi = novi_uidovi(mail, stanje.get("dan_uid", stanje["last_uid"]))
        izabrani = uidovi[-MAX_PREGLED:]
        mejlovi = [m for m in (dohvati_mejl(mail, u, MAX_TELA_PREGLED) for u in izabrani) if m]
    finally:
        mail.logout()

    if not uidovi:
        posalji_telegram(f"🌙 Danas ništa nije stiglo u inbox. {random.choice(FORE)}")
        osvezi_vesti(stanje)
        return 0

    try:
        pregled = gemini_pregled_dana(mejlovi, len(uidovi))
    except Exception as e:
        print(f"Gemini greška: {e} -> šaljem osnovni spisak")
        pregled = osnovni_sazetak(mejlovi)

    posalji_telegram(f"🌙 Pregled dana — {len(uidovi)} mejlova\n\n{pregled}")
    osvezi_vesti(stanje)
    stanje["last_uid"] = max(stanje["last_uid"], max(uidovi))
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
    global VESTI
    VESTI = stanje.get("vesti", [])
    sacuvaj_stanje(stanje)
    return stanje


def jedan_krug(stanje):
    """Jedna provera: fiksni sažeci (07/23) ili prag novih mejlova tokom dana."""
    sada = datetime.now(TZ)
    uradjeno = False

    # novi dan: zapamti odakle počinje (za večernji pregled celog dana)
    danas = f"{sada:%Y-%m-%d}"
    if stanje.get("dan") != danas:
        stanje["dan"] = danas
        stanje["dan_uid"] = stanje["last_uid"]
        sacuvaj_stanje(stanje)

    # 1) fiksni sažeci
    for sat, minut, vrsta in RASPORED:
        kljuc = f"{sada:%Y-%m-%d} {sat:02d}:{minut:02d}"
        zakazano = sada.replace(hour=sat, minute=minut, second=0, microsecond=0)
        kasni = (sada - zakazano).total_seconds() / 60
        if 0 <= kasni <= KASNJENJE_MIN and kljuc not in stanje["done"]:
            if vrsta == "pregled":
                posalji_pregled_dana(stanje)
            else:
                n = posalji_sazetak(f"Redovan sažetak u {sat:02d}:{minut:02d}", stanje)
                if n == 0:
                    posalji_telegram(f"📭 Nema novih mejlova ({sat:02d}:{minut:02d}), {random.choice(POZDRAVI).lower()}, možeš da dišeš.")
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

    elif arg == "--demo":
        # test: 3 izmišljena mejla -> pravi sažetak i pregled dana na Telegram (ne dira state.json)
        proveri_podesavanja("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY")
        lazni = [
            {"od": "TLDR AI <dan@tldrnewsletter.com>", "naslov": "TLDR AI 2026-11-16",
             "telo": "Nvidia i OpenAI nove cene, AI agenti u svakom alatu, top 10 prompt trikova..."},
            {"od": "Marko Jovanović <marko@digitalnastudija.rs>", "naslov": "Ponuda za sajt - treba odgovor do petka",
             "telo": "Ćao, šaljem ponudu za izradu sajta (1.200 EUR). Javi mi do petka da li ti odgovara, pa da krenemo."},
            {"od": "Shopify Deals <promo@deals-shop.com>", "naslov": "LAST CHANCE: 70% OFF samo danas!!!",
             "telo": "Don't miss out! Kupi sada i uštedi. Unsubscribe."},
        ]
        VESTI = dohvati_vesti()
        print("Vesti:", VESTI)
        posalji_telegram("🧪 TEST 1/2 (obični sažetak)\n\n" + gemini_sazetak(lazni, "Redovan sažetak u 07:00"))
        posalji_telegram("🧪 TEST 2/2 (večernji pregled)\n\n" + gemini_pregled_dana(lazni, 37))

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

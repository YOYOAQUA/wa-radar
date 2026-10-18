#!/usr/bin/env python3
"""WA Radar engine.

Reads WhatsApp Desktop's local database on a Mac (a fresh read-only COPY, never the
original) or chats exported from the phone, and prepares short, Claude-readable "packs":
who is waiting for an answer, leads, promises I made, requests in groups that match what
I sell, relationships that went quiet, a daily brief. Claude reads the packs, writes a
report.json, and `dashboard` turns it into a local HTML page with one-click reply drafts.

It never writes to WhatsApp and never sends anything.

  wr check                               what's here, can we read it, how much
  wr snapshot                            fresh read-only copy of the database
  wr waiting  [--days 60] [--limit 60]   chats where the last word is theirs
  wr leads    [--days 120] [--kw "a,b"]  new people + buying signals
  wr promises [--days 90]                things I said I'd do
  wr asks     [--days 60] [--kw "a,b"]   requests in groups (+ groups where I'm silent)
  wr cold     [--min 40] [--silent 60]   relationships that went quiet
  wr brief    [--hours 24] [--kw "a,b"] [--me "name,nick"]
  wr export   (--chat TEXT | --all) [--days 30] [--groups]
  wr voice    [--days 14] [--limit 60] [--groups]    transcribe voice notes (optional)
  wr voice-setup                         install the Hebrew speech model (once)
  wr import   FILE.zip|FILE.txt [--me NAME]          chat exported from the phone
  wr dashboard REPORT.json [--open]
  wr clean

Add --mask to any command for recording mode: names, numbers and group names are replaced
by stable aliases ("איש קשר 12", "קבוצה 3") before anything is printed or saved.
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HOME = Path(os.environ.get("WA_RADAR_HOME", Path.home() / "WaRadar")).expanduser()
SNAP, PACKS, REPORTS, IMPORTS, VOICE = (HOME / d for d in (".snap", "packs", "reports", "imports", "voice"))
WA_GROUP = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
DBS = ["ChatStorage.sqlite", "LID.sqlite", "ContactsV2.sqlite"]
APPLE = 978307200  # seconds between 1970 and 2001 (WhatsApp on Apple stores dates from 2001)
MODEL = os.environ.get("WA_RADAR_MODEL", "ivrit-ai/whisper-large-v3-turbo-ct2")
SNAP_MAX_AGE = 3600

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def say(msg):
    print(msg, flush=True)


def die(msg, code=1):
    say(msg)
    sys.exit(code)


# ---------------------------------------------------------------- text helpers
CLOSERS = {"תודה", "תודה רבה", "תודה!", "סבבה", "אחלה", "מעולה", "בסדר", "אוקיי", "אוקי", "ok", "okay", "יאללה",
           "לילה טוב", "שבת שלום", "חג שמח", "בהצלחה", "מושלם", "סגור", "קיבלתי", "יופי", "כיף", "אמן", "נהדר",
           "תודה גם לך", "גם לך", "בכיף", "סגרנו", "טוב", "בטח", "וואו", "חח", "חחח", "חחחח", "תודה אחי", "תודה יואב"}
ASK_RE = re.compile(r"\?|אפשר|תוכל|תוכלי|מתי|כמה|מחיר|פרטים|זמין|זמינה|תחזור|תחזרי|דחוף|מה דעתך|מה אתה אומר|"
                    r"מה את אומרת|רלוונטי|מעוניין|מעוניינת|אשמח|לקבוע|נדבר|התקשרתי|מחכה|ממתין|ממתינה")
LEAD_WORDS = ["מחיר", "הצעת מחיר", "כמה עולה", "עלות", "פרטים", "להזמין", "הזמנה", "זמין", "זמינה", "תאריך",
              "אירוע", "סדנה", "סדנא", "הרצאה", "שיתוף פעולה", "חסות", "קמפיין", "תקציב", "פגישה", "שיחת היכרות",
              "מעוניין", "מעוניינת", "מתעניין", "מתעניינת", "הצעה", "לקבוע", "ליווי", "קורס", "גיבוש", "יום כיף"]
PROMISE_RE = re.compile(r"אשלח|אבדוק|אחזור אלי|אחזור לך|אחזור אל|אעדכן|אתקשר|אכין|אסדר|אעביר|אדבר עם|אני על זה|"
                        r"בטיפול|אטפל|מחר אני|נדבר מחר|אכתוב לך|אחזיר|אארגן|אברר|אסגור|אחשוב על זה|אשמח לשלוח|"
                        r"אני אבדוק|אני אשלח|תן לי לבדוק|תני לי לבדוק|אחזור עם תשובה|עד סוף השבוע|בשבוע הבא אני")
REQUEST_RE = re.compile(r"מישהו מכיר|מישהי מכירה|מישהו יכול|מישהי יכולה|מכירים|מכירות|ממליצים|ממליצות|המלצה|"
                        r"המלצות|מחפש|מחפשת|מחפשים|מחפשות|למישהו יש|אשמח להמלצ|אשמח לחיבור|אשמח לשמוע|"
                        r"צריכים|צריכות|מי מכיר|מי מכירה|מי יודע|מי יודעת|יש למישהו|רלוונטי למישהו")
AUTO_RE = re.compile(r"לא קיבלנו את תגובתך|פנייתך נסגרה|הפניה תיסגר|נציג יתפנה|נציג שירות|שירות הלקוחות|"
                     r"יש להשיב להודעה זו|להסרה|הסרה מרשימת|השב הסר|השיבו הסר|לחצו כאן|לחץ כאן|בלחיצה אחת|unsubscribe|AI של|"
                     r"הודעה אוטומטית|קוד האימות|קוד אימות|verification code|מספר הזמנה|חשבונית|תזכורת לתור|"
                     r"לאישור השב|לביטול השב|השב 1|השיבו 1|wa\.me/|bit\.ly|לפרטים נוספים לחצו", re.I)
_SCRUB = lambda t: t  # replaced by Model.scrub in recording mode
PHONE_RE = re.compile(r"(?<!\d)(?:\+?972[\s-]?|0)(?:\d[\s-]?){8,9}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


B64_NAME = re.compile(r"^\+[A-Za-z0-9+/]{3,}={0,2}$")


def clean_name(v):
    """Some names are stored as base64 protobuf ('+ChHX...'). Pull the readable name out."""
    if not v or not B64_NAME.match(v):
        return v or ""
    try:
        import base64
        b = v[1:]
        raw = base64.b64decode(b + "=" * (-len(b) % 4))
        if raw[:1] == b"\x0a" and len(raw) >= 2 and 2 + raw[1] <= len(raw):
            return raw[2:2 + raw[1]].decode("utf-8", "ignore").strip()
    except Exception:
        pass
    return v


def norm(s):
    return re.sub(r"[^\w\s]", "", (s or "").lower()).strip()


def is_closer(text):
    t = norm(text)
    if not t:
        return True  # emoji, sticker, reaction-like
    return t in {norm(c) for c in CLOSERS} or (len(t.split()) <= 2 and t.split()[0] in {"תודה", "סבבה", "אחלה", "מעולה"})


def when(ts, now=None):
    d = dt.datetime.fromtimestamp(ts)
    now = now or dt.datetime.now()
    return d.strftime("%d.%m %H:%M") if d.year == now.year else d.strftime("%d.%m.%y %H:%M")


def ago_days(ts):
    return max(0, int((time.time() - ts) // 86400))


def ago_txt(ts):
    d = ago_days(ts)
    if d == 0:
        h = int((time.time() - ts) // 3600)
        return "היום" if h < 1 else f"לפני {h} שעות"
    return "אתמול" if d == 1 else f"לפני {d} ימים"


def local_phone(digits):
    if not digits:
        return ""
    d = re.sub(r"\D", "", digits)
    if d.startswith("972") and len(d) >= 11:
        d = "0" + d[3:]
        return f"{d[:3]}-{d[3:]}"
    return "+" + d


def kw_list(s):
    return [k.strip() for k in (s or "").split(",") if k.strip()]


def looks_auto(theirs, never_wrote):
    """Bots, shops, reminders: things nobody is really waiting on."""
    if not theirs:
        return False
    hits = sum(1 for m in theirs if AUTO_RE.search(m["text"]))
    if hits and (never_wrote or hits * 2 >= len(theirs)):
        return True
    avg = sum(len(m["text"]) for m in theirs) / len(theirs)
    return never_wrote and len(theirs) >= 3 and avg > 180


def cut(s, n=500):
    s = (s or "").replace("\r", "").strip()
    s = re.sub(r"\n{2,}", "\n", s)
    return s if len(s) <= n else s[:n].rstrip() + "…"


# ---------------------------------------------------------------- snapshot
def wa_present():
    return (WA_GROUP / "ChatStorage.sqlite").exists()


def snapshot(quiet=False):
    if not wa_present():
        if not quiet:
            say("WhatsApp Desktop database not found on this computer (Mac only). Exported chats still work: wr import FILE")
        return False
    SNAP.mkdir(parents=True, exist_ok=True)
    try:
        for db in DBS:
            src = WA_GROUP / db
            if not src.exists():
                continue
            for suf in ("-wal", "-shm"):
                (SNAP / (db + suf)).unlink(missing_ok=True)
            shutil.copy2(src, SNAP / db)
            for suf in ("-wal", "-shm"):
                if (WA_GROUP / (db + suf)).exists():
                    shutil.copy2(WA_GROUP / (db + suf), SNAP / (db + suf))
    except PermissionError:
        die("PERMISSION: macOS blocked reading WhatsApp's folder. Fix: System Settings > Privacy & Security > "
            "Full Disk Access > turn on Claude (or the app running this), then quit and reopen it.", 2)
    (SNAP / "taken.txt").write_text(dt.datetime.now().isoformat(timespec="seconds"))
    if not quiet:
        say(f"snapshot ok: read-only copy in {SNAP} (WhatsApp itself was not touched)")
    return True


def ensure_snapshot(refresh=True):
    taken = SNAP / "taken.txt"
    fresh = taken.exists() and time.time() - taken.stat().st_mtime < SNAP_MAX_AGE
    if refresh and not fresh and wa_present():
        snapshot(quiet=True)
    return (SNAP / "ChatStorage.sqlite").exists()


# ---------------------------------------------------------------- the model
class Model:
    """Everything the commands need, from the DB copy and/or imported chats."""

    def __init__(self, mask=False, refresh=True):
        self.mask = mask
        self.has_db = ensure_snapshot(refresh)
        self.chats = {}
        self.members = {}
        self._alias = self._load_aliases()
        self.lid, self.contact_lid, self.contact_phone, self.push = {}, {}, {}, {}
        self.voice_text = self._load_voice()
        if self.has_db:
            self.db = sqlite3.connect(SNAP / "ChatStorage.sqlite")
            self._load_people()
            self._load_chats()
        self._load_imports()
        self._names_re = None
        global _SCRUB
        _SCRUB = self.scrub if mask else (lambda t: t)

    # ---- people
    def _load_people(self):
        try:
            l = sqlite3.connect(SNAP / "LID.sqlite")
            for ident, phone, disp in l.execute("select ZIDENTIFIER, ZPHONENUMBER, ZDISPLAYNAME from ZWAZACCOUNT"):
                if ident:
                    self.lid[ident] = (re.sub(r"\D", "", phone or ""), disp or "")
        except sqlite3.Error:
            pass
        try:
            k = sqlite3.connect(SNAP / "ContactsV2.sqlite")
            for lid, phone, full, given, last in k.execute(
                    "select ZLID, ZPHONENUMBER, ZFULLNAME, ZGIVENNAME, ZLASTNAME from ZWAADDRESSBOOKCONTACT"):
                name = (full or " ".join(x for x in (given, last) if x)).strip()
                if not name:
                    continue
                if lid:
                    self.contact_lid[lid] = name
                d = re.sub(r"\D", "", phone or "")
                if d:
                    self.contact_phone[d] = name
        except sqlite3.Error:
            pass
        for jid, name in self.db.execute("select ZJID, ZPUSHNAME from ZWAPROFILEPUSHNAME"):
            name = clean_name(name)
            if jid and name:
                self.push[jid] = name
        for pk, jid, cname, fname in self.db.execute(
                "select Z_PK, ZMEMBERJID, ZCONTACTNAME, ZFIRSTNAME from ZWAGROUPMEMBER"):
            self.members[pk] = (jid or "", clean_name(cname), clean_name(fname))

    def phone_of(self, jid):
        if not jid:
            return ""
        if jid.endswith("@s.whatsapp.net"):
            return jid.split("@")[0]
        if jid.endswith("@lid"):
            return self.lid.get(jid, ("", ""))[0]
        return ""

    def person(self, jid, hint=""):
        """Best human name for a person jid: saved contact > hint > their WhatsApp name > number."""
        phone = self.phone_of(jid)
        name = ""
        for cand in (self.contact_lid.get(jid), phone and self.contact_phone.get(phone), hint,
                     self.push.get(jid), self.lid.get(jid, ("", ""))[1]):
            name = clean_name(cand or "")
            if name and not B64_NAME.match(name):
                break
            name = ""
        if not name and phone:
            name = local_phone(phone)
        return (name or "לא ידוע"), phone

    # ---- chats
    def _load_chats(self):
        q = ("select Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, ZARCHIVED, ZUNREADCOUNT, ZHIDDEN "
             "from ZWACHATSESSION where ZSESSIONTYPE in (0,1) and coalesce(ZREMOVED,0)=0")
        for pk, jid, partner, stype, arch, unread, hidden in self.db.execute(q):
            if hidden:  # locked chats stay locked
                continue
            kind = "group" if stype == 1 else "private"
            if kind == "private":
                name, phone = self.person(jid, partner or "")
                if partner and not self.contact_lid.get(jid) and not self.contact_phone.get(phone or "-"):
                    name = partner
            else:
                name, phone = partner or "קבוצה", ""
            self.chats[f"c{pk}"] = {"id": f"c{pk}", "pk": pk, "jid": jid or "", "name": name, "kind": kind,
                                    "phone": phone, "archived": bool(arch), "unread": unread or 0}

    def _load_imports(self):
        if not IMPORTS.exists():
            return
        for i, f in enumerate(sorted(IMPORTS.glob("*.json")), 1):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            cid = f"i{i}"
            self.chats[cid] = {"id": cid, "pk": None, "jid": "import:" + f.stem, "name": d["name"], "kind": d["kind"],
                               "phone": d.get("phone", ""), "archived": False, "unread": 0, "msgs": d["messages"]}

    # ---- aliases (recording mode)
    def _load_aliases(self):
        p = HOME / ".mask.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_aliases(self):
        if self.mask:
            HOME.mkdir(parents=True, exist_ok=True)
            (HOME / ".mask.json").write_text(json.dumps(self._alias, ensure_ascii=False), encoding="utf-8")

    def alias(self, key, kind):
        if key not in self._alias:
            n = sum(1 for v in self._alias.values() if v.startswith("קבוצה" if kind == "group" else "איש קשר")) + 1
            self._alias[key] = f"קבוצה {n}" if kind == "group" else f"איש קשר {n}"
        return self._alias[key]

    def chat_name(self, c):
        return self.alias(c["jid"], c["kind"]) if self.mask else c["name"]

    def who_name(self, name, jid):
        return self.alias(jid or name, "private") if self.mask else name

    def phone_txt(self, phone):
        if not phone:
            return ""
        return ("05X-XXX-XX" + phone[-2:]) if self.mask else local_phone(phone)

    def scrub(self, text):
        if not self.mask or not text:
            return text
        text = PHONE_RE.sub("[מספר]", text)
        text = EMAIL_RE.sub("[מייל]", text)
        if self._names_re is None:
            names = {c["name"] for c in self.chats.values() if c["kind"] == "private"}
            names |= set(self.contact_lid.values()) | set(self.contact_phone.values()) | set(self.push.values())
            names |= {m[1] for m in self.members.values() if m[1]} | {m[2] for m in self.members.values() if m[2]}
            full, first = set(), set()
            for n in names:
                n = (n or "").strip()
                if not n or re.fullmatch(r"[\d\s+\-~]+", n):
                    continue
                if " " in n and len(n) >= 4:
                    full.add(n)
                tok = re.split(r"[\s,.()|\-]+", n)[0]
                if len(tok) >= 3 and re.fullmatch(r"[\u05D0-\u05EAA-Za-z']+", tok):
                    first.add(tok)
            alt = "|".join(map(re.escape, sorted(full | first, key=len, reverse=True)))
            # a name may carry a Hebrew prefix letter: לדנה, ודני, שמיכל
            self._names_re = re.compile(r"(?<![\w\u0590-\u05FF])([ולבהשמכ]{0,2})(?:" + alt + r")(?![\w\u0590-\u05FF])") if alt else False
        if self._names_re:
            text = self._names_re.sub(lambda m: m.group(1) + "[שם]", text)
        return text

    # ---- voice cache
    def _load_voice(self):
        p = VOICE / "transcripts.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    # ---- messages
    def render(self, mtype, text, title, dur, vcard, pk):
        if mtype in (0, 7):
            return text or ""
        if mtype == 1:
            return "[תמונה]" + (f" {title}" if title else "")
        if mtype == 2:
            return "[סרטון]" + (f" {title}" if title else "")
        if mtype == 3:
            m, s = divmod(int(dur or 0), 60)
            t = self.voice_text.get(str(pk))
            return f"[הקלטה קולית {m}:{s:02d}]" + (f" «{t}»" if t else "")
        if mtype == 4:
            return f"[איש קשר: {vcard or ''}]"
        if mtype == 5:
            return "[מיקום]"
        if mtype == 8:
            return f"[מסמך: {title or ''}]" + (f" {text}" if text and text != title else "")
        if mtype == 11:
            return "[GIF]"
        if mtype == 46:
            return "[סקר]" + (f" {text}" if text else "")
        return text or ""  # unknown types: keep text if any, otherwise dropped below

    def messages(self, since=0, chat_ids=None, kinds=("private", "group"), until=None):
        """List of message dicts, oldest first, grouped by chat order of appearance."""
        out = []
        until = until or time.time() + 86400
        db_ids = [c for c in (chat_ids or self.chats) if c.startswith("c") and self.chats.get(c, {}).get("kind") in kinds]
        if self.has_db and db_ids:
            pks = {self.chats[c]["pk"]: self.chats[c] for c in db_ids}
            q = ("select m.Z_PK, m.ZCHATSESSION, m.ZMESSAGEDATE, m.ZISFROMME, m.ZMESSAGETYPE, m.ZTEXT, m.ZGROUPMEMBER, "
                 "mi.ZTITLE, mi.ZMOVIEDURATION, mi.ZVCARDNAME from ZWAMESSAGE m left join ZWAMEDIAITEM mi "
                 "on mi.Z_PK = m.ZMEDIAITEM where m.ZMESSAGEDATE > ? and m.ZMESSAGEDATE < ? "
                 "and m.ZMESSAGETYPE not in (6, 10, 14, 15, 59, 66) order by m.ZCHATSESSION, m.ZMESSAGEDATE")
            for pk, cpk, md, me, mt, text, gm, title, dur, vcard in self.db.execute(q, (since - APPLE, until - APPLE)):
                c = pks.get(cpk)
                if not c:
                    continue
                body = self.render(mt, text, title, dur, vcard, pk)
                if not body.strip():
                    continue
                ref = None
                if me:
                    who = "אני"
                elif c["kind"] == "private":
                    who = self.chat_name(c)
                else:
                    jid, cname, fname = self.members.get(gm, ("", "", ""))
                    name, _ = self.person(jid, cname or fname)
                    who, ref = self.who_name(name, jid), (f"u{gm}" if gm else None)
                out.append({"id": pk, "chat": c["id"], "ts": md + APPLE, "me": bool(me), "who": who, "ref": ref,
                            "type": mt, "text": body})
        for cid in (chat_ids or self.chats):
            c = self.chats.get(cid)
            if not c or "msgs" not in c or c["kind"] not in kinds:
                continue
            for j, m in enumerate(c["msgs"]):
                if since < m["ts"] < until:
                    who = "אני" if m["me"] else (self.chat_name(c) if c["kind"] == "private" else self.who_name(m["who"], m["who"]))
                    out.append({"id": f"{cid}-{j}", "chat": cid, "ts": m["ts"], "me": m["me"], "who": who, "ref": None,
                                "type": 0, "text": m["text"]})
        return out

    def chat_stats(self):
        """Per chat, all time: total messages, mine, first and last timestamp."""
        st = {}
        if self.has_db:
            q = ("select ZCHATSESSION, count(*), sum(ZISFROMME), min(ZMESSAGEDATE), max(ZMESSAGEDATE) from ZWAMESSAGE "
                 "where ZMESSAGETYPE not in (6, 10, 14, 15, 59, 66) group by ZCHATSESSION")
            for cpk, n, mine, first, last in self.db.execute(q):
                st[f"c{cpk}"] = {"n": n, "mine": mine or 0, "first": (first or 0) + APPLE, "last": (last or 0) + APPLE}
        for cid, c in self.chats.items():
            if "msgs" in c and c["msgs"]:
                ms = c["msgs"]
                st[cid] = {"n": len(ms), "mine": sum(1 for m in ms if m["me"]), "first": ms[0]["ts"], "last": ms[-1]["ts"]}
        return st

    def ref_phone(self, ref):
        """Phone digits for a report reference: c123 (private chat), u456 (group member), i2 (import)."""
        if not ref:
            return ""
        if ref.startswith("u") and ref[1:].isdigit():
            jid = self.members.get(int(ref[1:]), ("",))[0]
            return self.phone_of(jid)
        c = self.chats.get(ref)
        return (c or {}).get("phone", "") if (c or {}).get("kind") == "private" else ""


def by_chat(msgs):
    g = {}
    for m in msgs:
        g.setdefault(m["chat"], []).append(m)
    return g


def txt(m, n=500):
    return cut(_SCRUB(m["text"]), n)


def line(m, n=500):
    return f"[{when(m['ts'])}] {m['who']}: {txt(m, n)}"


def head(M, c, extra=""):
    ph = M.phone_txt(c.get("phone")) if c["kind"] == "private" else ""
    kind = "פרטי" if c["kind"] == "private" else "קבוצה"
    bits = [M.chat_name(c), kind] + ([ph] if ph else []) + ([extra] if extra else [])
    return f"## [{c['id']}] " + " · ".join(bits)


def write_pack(name, title, intro, blocks, M):
    PACKS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    mode = " · מצב הקלטה (שמות מוסתרים)" if M.mask else ""
    body = f"# {title}\nנוצר {stamp}{mode}\n\n{intro}\n\n" + "\n\n".join(blocks) + "\n"
    p = PACKS / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    M.save_aliases()
    say(f"{name}: {len(blocks)} items -> {p}  ({len(body)//1000}K chars)")
    return p


# ---------------------------------------------------------------- commands
def cmd_check(a):
    say(f"home: {HOME}")
    say(f"system: {sys.platform}")
    say(f"WhatsApp Desktop database: {'found' if wa_present() else 'not found'}")
    if wa_present():
        snapshot(quiet=True)
    M = Model(refresh=False)
    if M.has_db:
        n_priv = sum(1 for c in M.chats.values() if c["kind"] == "private" and c["pk"])
        n_grp = sum(1 for c in M.chats.values() if c["kind"] == "group" and c["pk"])
        first, last = M.db.execute("select min(ZMESSAGEDATE), max(ZMESSAGEDATE) from ZWAMESSAGE where ZMESSAGEDATE > 0").fetchone()
        n30 = M.db.execute("select count(*) from ZWAMESSAGE where ZMESSAGEDATE > ?", (time.time() - 30 * 86400 - APPLE,)).fetchone()[0]
        v30 = M.db.execute("select count(*) from ZWAMESSAGE where ZMESSAGETYPE=3 and ZMESSAGEDATE > ?",
                           (time.time() - 30 * 86400 - APPLE,)).fetchone()[0]
        say(f"chats: {n_priv} private, {n_grp} groups (locked chats skipped)")
        say(f"history: {dt.date.fromtimestamp(first + APPLE)} -> {dt.date.fromtimestamp(last + APPLE)}")
        say(f"last 30 days: {n30} messages, {v30} voice notes")
        mapped = sum(1 for c in M.chats.values() if c["kind"] == "private" and c["phone"])
        say(f"reply links possible for {mapped}/{n_priv} private chats")
    n_imp = sum(1 for c in M.chats.values() if "msgs" in c)
    say(f"imported chats: {n_imp}")
    try:
        import faster_whisper  # noqa: F401
        say(f"voice notes: speech model engine installed ({len(M.voice_text)} transcribed so far)")
    except Exception:
        say("voice notes: not set up (optional: wr voice-setup)")
    prof = HOME / "profile.md"
    say(f"profile: {'ok' if prof.exists() else 'missing (create ' + str(prof) + ')'}")


def cmd_snapshot(a):
    if not snapshot():
        sys.exit(1)


def cmd_waiting(a):
    M = Model(a.mask)
    since = time.time() - a.days * 86400
    st = M.chat_stats()
    g = by_chat(M.messages(since, kinds=("private",)))
    items, skipped = [], 0
    for cid, ms in g.items():
        if ms[-1]["me"]:
            continue
        run = []
        for m in reversed(ms):
            if m["me"]:
                break
            run.append(m)
        run.reverse()
        if all(is_closer(m["text"]) for m in run):
            continue
        joined = " ".join(m["text"] for m in run)
        mine = st.get(cid, {}).get("mine", 0)
        never = mine == 0
        if looks_auto([m for m in ms if not m["me"]], never):
            skipped += 1
            continue
        waited = ago_days(run[0]["ts"])
        asks = bool(ASK_RE.search(joined))
        score = (3 if asks else 0) + max(0, 3 - waited / 10) + min(mine, 200) ** 0.5 / 5 \
            + (1.5 if never and asks else 0) + (1 if M.chats[cid]["unread"] else 0) + min(len(run), 4) / 2
        items.append((score, cid, run, never, waited, ms))
    items.sort(key=lambda x: -x[0])
    blocks = []
    for score, cid, run, never, waited, ms in items[:a.limit]:
        c = M.chats[cid]
        flags = [f"מחכה {waited} ימים" if waited else "מחכה מהיום", f"{len(run)} הודעות ממתינות"]
        if never:
            flags.append("לא כתבת לו/לה אף פעם")
        if ASK_RE.search(" ".join(m["text"] for m in run)):
            flags.append("יש שאלה או בקשה")
        ctx = ms[-8:]
        blocks.append(head(M, c, " · ".join(flags)) + "\n" + "\n".join(line(m) for m in ctx))
    intro = (f"שיחות פרטיות מ-{a.days} הימים האחרונים שבהן המילה האחרונה היא שלהם (בלי 'תודה' ו'סבבה'). "
             f"{len(items)} שיחות, מוצגות {min(len(items), a.limit)} לפי דחיפות "
             f"(שאלה פתוחה, כמה זמן מחכים, כמה הקשר קרוב). {skipped} שיחות עם בוטים והודעות אוטומטיות סוננו. "
             "שימו לב: חלק אולי נסגרו בטלפון או בהקלטה. עסקים ושירותים שעוד נשארו, מדלגים.")
    write_pack("waiting", "מחכים לתשובה ממך", intro, blocks, M)


def cmd_leads(a):
    M = Model(a.mask)
    since = time.time() - a.days * 86400
    st = M.chat_stats()
    words = LEAD_WORDS + kw_list(a.kw)
    wre = re.compile("|".join(map(re.escape, words)))
    g = by_chat(M.messages(since, kinds=("private",)))
    items = []
    for cid, ms in g.items():
        s = st.get(cid, {})
        if looks_auto([m for m in ms if not m["me"]], s.get("mine", 0) == 0):
            continue
        new = s.get("first", 0) >= since and not ms[0]["me"]
        hits = [i for i, m in enumerate(ms) if not m["me"] and wre.search(m["text"])]
        if not new and not hits:
            continue
        idx = hits[-1] if hits else 0
        answered = any(m["me"] for m in ms[idx + 1:])
        ctx = ms[max(0, idx - 3): idx + 5] if hits else ms[:6]
        found = sorted({w for m in ctx if not m["me"] for w in words if w in m["text"]})[:6]
        score = (3 if not answered else 0) + (2 if new else 0) + len(found)
        items.append((score, cid, new, answered, found, ctx, ms[-1]["ts"]))
    items.sort(key=lambda x: (-x[0], -x[6]))
    blocks = []
    for score, cid, new, answered, found, ctx, last in items[:a.limit]:
        flags = (["פנייה חדשה"] if new else []) + (["לא ענית" if not answered else "ענית"]) + \
                ([("מילים: " + ", ".join(found))] if found else []) + [f"אחרונה {ago_txt(last)}"]
        blocks.append(head(M, M.chats[cid], " · ".join(flags)) + "\n" + "\n".join(line(m) for m in ctx))
    intro = (f"פניות מ-{a.days} הימים האחרונים: אנשים חדשים שפנו אליך, ושיחות עם מילים של קנייה "
             f"({', '.join(words[:12])}...). {len(items)} שיחות, מוצגות {min(len(items), a.limit)}. "
             "'לא ענית' = אין הודעה שלך אחרי הפנייה.")
    write_pack("leads", "לידים והזדמנויות בפרטי", intro, blocks, M)


def cmd_promises(a):
    M = Model(a.mask)
    since = time.time() - a.days * 86400
    g = by_chat(M.messages(since))
    items = []
    for cid, ms in g.items():
        for i, m in enumerate(ms):
            if not m["me"] or not PROMISE_RE.search(m["text"]):
                continue
            after = ms[i + 1:]
            mine_after = [x for x in after if x["me"]]
            items.append((0 if mine_after else 1, m["ts"], cid, ms[max(0, i - 2): i + 1], after[:5], bool(mine_after)))
    items.sort(key=lambda x: (-x[0], -x[1]))
    blocks = []
    for _, ts, cid, before, after, followed in items[:a.limit]:
        flag = "כתבת בשיחה אחרי זה (לבדוק אם קיימת)" if followed else "אחרי ההבטחה לא כתבת בשיחה"
        body = "\n".join(line(m) for m in before) + "\n  ↓ אחרי זה:\n" + ("\n".join(line(m) for m in after) or "  (כלום)")
        blocks.append(head(M, M.chats[cid], f"{ago_txt(ts)} · {flag}") + "\n" + body)
    intro = (f"הודעות שלך מ-{a.days} הימים האחרונים שנשמעות כמו התחייבות ('אשלח', 'אבדוק', 'אחזור אליך'...), "
             f"ומה קרה אחריהן. {len(items)} נמצאו, מוצגות {min(len(items), a.limit)}. קודם אלה שלא כתבת אחריהן כלום.")
    write_pack("promises", "הבטחות שנתת", intro, blocks, M)


def cmd_asks(a):
    M = Model(a.mask)
    since = time.time() - a.days * 86400
    kws = kw_list(a.kw)
    kre = re.compile("|".join(map(re.escape, kws))) if kws else None
    g = by_chat(M.messages(since, kinds=("group",)))
    items, quiet = [], []
    for cid, ms in g.items():
        mine = sum(1 for m in ms if m["me"])
        reqs = 0
        for i, m in enumerate(ms):
            if m["me"] or len(m["text"]) < 20 or not REQUEST_RE.search(m["text"]):
                continue
            reqs += 1
            if kre and not kre.search(m["text"]):
                continue
            replied = any(x["me"] and x["ts"] - m["ts"] < 2 * 86400 for x in ms[i + 1:])
            items.append((m["ts"], cid, m, replied))
        if len(ms) >= 150 and mine <= 2:
            quiet.append((len(ms), cid, mine, reqs))
    items.sort(key=lambda x: -x[0])
    blocks = []
    for ts, cid, m, replied in items[:a.limit]:
        ref = f" [{m['ref']}]" if m["ref"] else ""
        blocks.append(f"## [{cid}] {M.chat_name(M.chats[cid])} · {ago_txt(ts)} · "
                      f"{'ענית בקבוצה' if replied else 'לא ענית'}\n[{when(m['ts'])}] {m['who']}{ref}: {txt(m, 700)}")
    quiet.sort(key=lambda x: -x[0])
    qb = "\n".join(f"- [{cid}] {M.chat_name(M.chats[cid])}: {n} הודעות, {mine} שלך, {r} בקשות להמלצה"
                   for n, cid, mine, r in quiet[:25])
    if qb:
        blocks.append("## קבוצות פעילות שבהן אתה כמעט לא כותב\n" + qb)
    flt = f"שקשורות ל: {', '.join(kws)}" if kws else "(בלי סינון לפי תחום. כדאי להריץ עם --kw)"
    intro = (f"בקשות והמלצות בקבוצות מ-{a.days} הימים האחרונים {flt}. {len(items)} נמצאו. "
             "הסימון [u123] ליד השם מאפשר טיוטת פנייה אישית בפרטי (בדשבורד: \"reply_to\": \"u123\").")
    write_pack("asks", "בקבוצות מבקשים את מה שאתה עושה", intro, blocks, M)


def cmd_cold(a):
    M = Model(a.mask)
    st = M.chat_stats()
    now = time.time()
    rows = []
    for cid, s in st.items():
        c = M.chats.get(cid)
        if not c or c["kind"] != "private":
            continue
        if s["n"] >= a.min and s["mine"] >= 10 and a.silent * 86400 < now - s["last"] < 730 * 86400:
            rows.append((s["n"], cid, s))
    rows.sort(key=lambda x: -x[0])
    blocks = []
    for n, cid, s in rows[:a.limit]:
        last = M.messages(s["last"] - 30 * 86400, chat_ids=[cid])[-3:]
        blocks.append(head(M, M.chats[cid], f"{n} הודעות לאורך הזמן · מאז {dt.date.fromtimestamp(s['first']).strftime('%m.%Y')} · "
                                             f"שקט {ago_days(s['last'])} ימים") + "\n" + "\n".join(line(m, 300) for m in last))
    intro = (f"אנשים שהיה איתם קשר משמעותי (לפחות {a.min} הודעות, ולפחות 10 שלך), ושקט כבר יותר מ-{a.silent} ימים. "
             f"{len(rows)} נמצאו, מוצגים {min(len(rows), a.limit)}, מהקשר הכי חזק.")
    write_pack("cold", "קשרים שהתקררו", intro, blocks, M)


def cmd_brief(a):
    M = Model(a.mask)
    since = time.time() - a.hours * 3600
    kws = kw_list(a.kw)
    me = [n for n in kw_list(a.me) if len(n) >= 2]
    kre = re.compile("|".join(map(re.escape, kws))) if kws else None
    mre = re.compile("|".join(map(re.escape, me))) if me else None
    g = by_chat(M.messages(since - 3 * 86400))
    priv, grp = [], []
    for cid, ms in g.items():
        new = [m for m in ms if m["ts"] >= since]
        if not new:
            continue
        c = M.chats[cid]
        if c["kind"] == "private":
            ctx = [m for m in ms if m["ts"] < since][-2:]
            priv.append((new[-1]["ts"], cid, ctx, new))
        else:
            hits = [m for m in new if not m["me"] and ((kre and kre.search(m["text"]) and REQUEST_RE.search(m["text"]))
                                                       or (mre and mre.search(m["text"])))]
            grp.append((len(new), cid, hits))
    priv.sort(key=lambda x: -x[0])
    blocks = []
    for ts, cid, ctx, new in priv[:a.limit]:
        last_me = new[-1]["me"]
        body = "\n".join("  (לפני) " + line(m, 200) for m in ctx) + ("\n" if ctx else "") + "\n".join(line(m) for m in new[-12:])
        blocks.append(head(M, M.chats[cid], ("ענית" if last_me else "ממתין לך") + f" · {len(new)} חדשות") + "\n" + body)
    grp.sort(key=lambda x: -x[0])
    gl = []
    for n, cid, hits in grp[:30]:
        s = f"- [{cid}] {M.chat_name(M.chats[cid])}: {n} חדשות"
        for m in hits[:5]:
            ref = f" [{m['ref']}]" if m["ref"] else ""
            s += f"\n    ★ [{when(m['ts'])}] {m['who']}{ref}: {txt(m, 300)}"
        gl.append(s)
    if gl:
        blocks.append("## בקבוצות (★ = מזכירים אותך, או מבקשים משהו שקשור אליך)\n" + "\n".join(gl))
    intro = f"כל מה שקרה ב-{a.hours} השעות האחרונות: {len(priv)} שיחות פרטיות, {len(grp)} קבוצות."
    write_pack("brief", "תדריך בוקר", intro, blocks, M)


def cmd_export(a):
    M = Model(a.mask)
    since = time.time() - a.days * 86400
    if a.chat:
        q = a.chat.lower()
        ids = [cid for cid, c in M.chats.items() if q in c["name"].lower() or q == cid]
        if not ids:
            die(f"no chat matches '{a.chat}'")
    elif a.all:
        ids = [cid for cid, c in M.chats.items() if c["kind"] == "private" or a.groups]
    else:
        die("use --chat TEXT or --all")
    g = by_chat(M.messages(since, chat_ids=ids))
    blocks = [head(M, M.chats[cid], f"{len(ms)} הודעות") + "\n" + "\n".join(line(m, 2000) for m in ms)
              for cid, ms in sorted(g.items(), key=lambda kv: -kv[1][-1]["ts"])]
    if not blocks:
        die("no messages in that range (try --days 365)")
    parts, cur = [], []
    for b in blocks:
        if cur and sum(map(len, cur)) + len(b) > 120_000:
            parts.append(cur)
            cur = []
        cur.append(b)
    parts.append(cur)
    for i, p in enumerate(parts, 1):
        name = "export" if len(parts) == 1 else f"export-{i}"
        write_pack(name, f"ייצוא שיחות · {a.days} ימים" + (f" · חלק {i}/{len(parts)}" if len(parts) > 1 else ""),
                   f"{len(g)} שיחות.", p, M)


def cmd_voice_setup(a):
    venv_py = sys.executable
    say("installing the speech engine (once)...")
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    r = subprocess.run([uv, "pip", "install", "--python", venv_py, "faster-whisper>=1.1"])
    if r.returncode:
        r = subprocess.run([venv_py, "-m", "pip", "install", "faster-whisper>=1.1"])
    if r.returncode:
        die("could not install faster-whisper")
    say(f"downloading the Hebrew model ({MODEL}, ~1.6GB, first time only)...")
    from faster_whisper import WhisperModel
    WhisperModel(MODEL, device="cpu", compute_type="int8")
    say("voice ready")


def cmd_voice(a):
    M = Model(a.mask)
    if not M.has_db:
        die("voice notes need WhatsApp Desktop on a Mac")
    try:
        from faster_whisper import WhisperModel
    except Exception:
        die("speech engine not installed. run: wr voice-setup")
    since = time.time() - a.days * 86400
    kinds = (0, 1) if a.groups else (0,)
    q = ("select m.Z_PK, mi.ZMEDIALOCALPATH, mi.ZMOVIEDURATION from ZWAMESSAGE m join ZWAMEDIAITEM mi on mi.Z_PK=m.ZMEDIAITEM "
         "join ZWACHATSESSION s on s.Z_PK=m.ZCHATSESSION where m.ZMESSAGETYPE=3 and m.ZMESSAGEDATE > ? "
         f"and s.ZSESSIONTYPE in ({','.join(map(str, kinds))}) and coalesce(s.ZHIDDEN,0)=0 order by m.ZMESSAGEDATE desc")
    todo = []
    for pk, rel, dur in M.db.execute(q, (since - APPLE,)):
        if str(pk) in M.voice_text or not rel:
            continue
        for base in (WA_GROUP / "Message", WA_GROUP):
            if (base / rel).exists():
                todo.append((pk, base / rel, dur or 0))
                break
    todo = todo[:a.limit]
    if not todo:
        say("nothing new to transcribe")
        return
    mins = sum(d for _, _, d in todo) / 60
    say(f"transcribing {len(todo)} voice notes (~{mins:.0f} min of audio). roughly real time on a laptop.")
    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    VOICE.mkdir(parents=True, exist_ok=True)
    cache = M.voice_text
    for i, (pk, path, dur) in enumerate(todo, 1):
        tmp = HOME / ".tmp_voice.opus"
        shutil.copy2(path, tmp)  # read from a copy, never from WhatsApp's folder directly
        try:
            segs, _ = model.transcribe(str(tmp), language="he", beam_size=1, vad_filter=True)
            cache[str(pk)] = " ".join(s.text.strip() for s in segs).strip()
        except Exception as e:
            cache[str(pk)] = ""
            say(f"  skip {pk}: {e}")
        tmp.unlink(missing_ok=True)
        (VOICE / "transcripts.json").write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        if i % 5 == 0 or i == len(todo):
            say(f"  {i}/{len(todo)}")
    say("done. voice notes now appear as text in every pack.")


HDR_RES = [
    # [18.10.2026, 0:12:34] Name: text      (iPhone)
    re.compile(r"^‎?\[(\d{1,2})[./](\d{1,2})[./](\d{2,4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s?([AaPp][Mm]))?\] ([^:]+?): (.*)$"),
    # 18.10.2026, 00:12 - Name: text        (Android)
    re.compile(r"^‎?(\d{1,2})[./](\d{1,2})[./](\d{2,4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s?([AaPp][Mm]))? [-–] ([^:]+?): (.*)$"),
]


def cmd_import(a):
    src = Path(a.file).expanduser()
    if not src.exists():
        die(f"not found: {src}")
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            txts = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not txts:
                die("no .txt inside the zip")
            raw = z.read(txts[0]).decode("utf-8", "replace")
    else:
        raw = src.read_text(encoding="utf-8", errors="replace")
    title = re.sub(r"^WhatsApp Chat (with |- |עם )?", "", src.stem).strip() or src.stem
    msgs = []
    for ln in raw.splitlines():
        ln = ln.replace("‏", "").replace("‪", "").replace("‬", "")
        m = None
        for r in HDR_RES:
            m = r.match(ln)
            if m:
                break
        if not m:
            if msgs and ln.strip():
                msgs[-1]["text"] += "\n" + ln
            continue
        d, mo, y, hh, mm, ss, ampm, who, text = m.groups()
        y = int(y) + (2000 if len(y) == 2 else 0)
        h = int(hh) % 12 + (12 if ampm and ampm.lower() == "pm" else 0) if ampm else int(hh)
        try:
            ts = dt.datetime(y, int(mo), int(d), h, int(mm), int(ss or 0)).timestamp()
        except ValueError:
            ts = dt.datetime(y, int(d), int(mo), h, int(mm), int(ss or 0)).timestamp()  # month/day order
        text = re.sub(r"^‎", "", text)
        if re.search(r"omitted|הושמט|לא נכלל|<Media|‎image|‎audio", text):
            text = "[מדיה]"
        msgs.append({"ts": ts, "who": who.strip(), "text": text})
    if not msgs:
        die("could not read messages from this file (unknown export format)")
    senders = {}
    for m in msgs:
        senders[m["who"]] = senders.get(m["who"], 0) + 1
    me = a.me
    if not me:
        others = [s for s in senders if s != title]
        me = others[0] if len(senders) == 2 and len(others) == 1 else ""
    for m in msgs:
        m["me"] = bool(me) and m["who"] == me
    kind = "private" if len(senders) <= 2 else "group"
    IMPORTS.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha1(title.encode()).hexdigest()[:8]
    (IMPORTS / f"{slug}.json").write_text(json.dumps({"name": title, "kind": kind, "me": me, "messages": msgs},
                                                     ensure_ascii=False), encoding="utf-8")
    say(f"imported '{title}': {len(msgs)} messages, {kind}, me = '{me or '?'}'")
    if not me:
        say("could not tell which sender is you. run again with --me \"your name as it appears in the file\"")


def cmd_clean(a):
    for p in (SNAP, PACKS, HOME / ".tmp_voice.opus"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    say("removed the database copy and packs. reports, profile, imports and voice transcripts were kept.")


# ---------------------------------------------------------------- dashboard
DASH = r"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<link href="https://fonts.googleapis.com/css2?family=Heebo:wght@400;700;900&display=swap" rel="stylesheet">
<style>
:root{--bg:#2B242A;--card:#352D34;--card2:#40373F;--gold:#F2B84B;--cream:#F6EAD0;--body:#D9CCB4;--sky:#BFCFDD;--line:rgba(246,234,208,.14);--hi:#FF7A6B;--md:#F2B84B;--lo:#8FD18B}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--cream);font-family:Heebo,Arial,sans-serif;line-height:1.5}
.wrap{max-width:1100px;margin:0 auto;padding:28px 18px 60px}
header h1{margin:0;font-size:clamp(26px,4vw,40px);font-weight:900}header h1 em{font-style:normal;color:var(--gold)}
.sub{color:var(--sky);margin-top:4px;font-size:15px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 16px}.stat b{display:block;font-size:24px;color:var(--gold)}.stat span{font-size:13px;color:var(--sky)}
.headline{background:linear-gradient(135deg,rgba(242,184,75,.18),rgba(242,184,75,.04));border:1px solid rgba(242,184,75,.45);border-radius:14px;padding:16px 18px;font-size:17px;white-space:pre-line}
nav{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 6px;position:sticky;top:0;background:var(--bg);padding:10px 0;z-index:5}
nav a{color:var(--cream);text-decoration:none;border:1px solid var(--line);border-radius:99px;padding:6px 14px;font-size:14px}nav a b{color:var(--gold)}
section{margin-top:30px}section h2{font-size:24px;margin:0 0 4px}section .note{color:var(--sky);font-size:14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:8px;transition:opacity .2s}
.card.done{opacity:.4}.card .top{display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;flex:none}.p-high{background:var(--hi)}.p-med{background:var(--md)}.p-low{background:var(--lo)}
.card .nm{font-weight:900;font-size:18px}.card .meta{margin-right:auto;font-size:13px;color:var(--sky)}
.card p{margin:0;color:var(--body);font-size:15px}.card .why{font-size:14px;color:var(--sky)}.card .val{color:var(--gold);font-weight:700;font-size:14px}
textarea{width:100%;min-height:60px;margin-top:auto;background:var(--card2);color:var(--cream);border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;font-size:15px;resize:vertical}
.acts{display:flex;flex-wrap:wrap;gap:8px}
.btn{border:0;border-radius:99px;padding:9px 14px;font:inherit;font-size:14px;font-weight:700;cursor:pointer}
.wa{background:#25D366;color:#0B2E17}.cp{background:var(--card2);color:var(--cream);border:1px solid var(--line)}.ok{background:none;color:var(--sky);text-decoration:underline;margin-right:auto}
.task{display:flex;gap:10px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px}
.task input{width:20px;height:20px;margin-top:3px;accent-color:var(--gold)}.task.done span{text-decoration:line-through;opacity:.5}.task small{display:block;color:var(--sky)}
footer{margin-top:50px;color:#8C7E8A;font-size:13px;text-align:center}
</style></head><body><div class="wrap" id="app"></div>
<script>
var R=__DATA__;
function e(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
var KEY='waradar_done_'+(R.title||'');var done={};try{done=JSON.parse(localStorage.getItem(KEY)||'{}');}catch(x){}
function save(){try{localStorage.setItem(KEY,JSON.stringify(done));}catch(x){}}
var h='<header><h1>'+e(R.title||'הרדאר')+'</h1><div class="sub">'+e(R.range||'')+(R.mask?' · מצב הקלטה, שמות ומספרים מוסתרים':'')+'</div></header>';
if(R.stats){h+='<div class="stats">';for(var k in R.stats){var v=R.stats[k];h+='<div class="stat"><b>'+e(typeof v==='number'?v.toLocaleString('he-IL'):v)+'</b><span>'+e(k)+'</span></div>';}h+='</div>';}
if(R.headline)h+='<div class="headline">'+e(R.headline)+'</div>';
h+='<nav>';(R.sections||[]).forEach(function(s,i){h+='<a href="#s'+i+'">'+e(s.icon||'')+' '+e(s.title)+' <b>'+(s.items||[]).length+'</b></a>';});h+='</nav>';
(R.sections||[]).forEach(function(s,i){
  h+='<section id="s'+i+'"><h2>'+e(s.icon||'')+' '+e(s.title)+'</h2>'+(s.note?'<div class="note">'+e(s.note)+'</div>':'');
  var tasks=(s.items||[]).filter(function(it){return !it.name;}),cards=(s.items||[]).filter(function(it){return it.name;});
  tasks.forEach(function(it,j){var id=i+'t'+j;h+='<label class="task'+(done[id]?' done':'')+'" data-id="'+id+'"><input type="checkbox"'+(done[id]?' checked':'')+'><span>'+e(it.text)+(it.due||it.from?'<small>'+e([it.due,it.from].filter(Boolean).join(' · '))+'</small>':'')+'</span></label>';});
  if(cards.length){h+='<div class="grid">';cards.forEach(function(it,j){var id=i+'c'+j;
    h+='<div class="card'+(done[id]?' done':'')+'" data-id="'+id+'"><div class="top"><i class="dot p-'+e(it.priority||'med')+'"></i><span class="nm">'+e(it.name)+'</span><span class="meta">'+e(it.meta||'')+'</span></div>';
    if(it.summary)h+='<p>'+e(it.summary)+'</p>';if(it.why)h+='<div class="why">'+e(it.why)+'</div>';if(it.value)h+='<div class="val">'+e(it.value)+'</div>';
    if(it.draft){h+='<textarea>'+e(it.draft)+'</textarea><div class="acts">'+(it._phone?'<button class="btn wa" data-p="'+e(it._phone)+'">פתחו בוואטסאפ עם הטיוטה</button>':'')+'<button class="btn cp">העתקה</button><button class="btn ok">'+(done[id]?'בטל סימון':'✓ טופל')+'</button></div>';}
    else h+='<div class="acts"><button class="btn ok">'+(done[id]?'בטל סימון':'✓ טופל')+'</button></div>';
    h+='</div>';});h+='</div>';}
  h+='</section>';});
h+='<footer>נוצר במחשב שלכם עם wa-radar · שום הודעה לא נשלחה בלי שלחצתם</footer>';
document.getElementById('app').innerHTML=h;
function fit(){[].forEach.call(document.querySelectorAll('textarea'),function(t){t.style.height='auto';t.style.height=(t.scrollHeight+4)+'px';});}
fit();if(document.fonts&&document.fonts.ready)document.fonts.ready.then(fit);
document.addEventListener('click',function(ev){var b=ev.target.closest('button');if(!b)return;var card=b.closest('.card');
  if(b.classList.contains('wa')){var t=card.querySelector('textarea').value;location.href='whatsapp://send?phone='+b.getAttribute('data-p')+'&text='+encodeURIComponent(t);}
  if(b.classList.contains('cp')){var ta=card.querySelector('textarea');ta.select();try{navigator.clipboard.writeText(ta.value);}catch(x){document.execCommand('copy');}b.textContent='✓ הועתק';setTimeout(function(){b.textContent='העתקה';},1500);}
  if(b.classList.contains('ok')){var id=card.getAttribute('data-id');done[id]=!done[id];save();card.classList.toggle('done',!!done[id]);b.textContent=done[id]?'בטל סימון':'✓ טופל';}});
document.addEventListener('change',function(ev){var l=ev.target.closest('.task');if(!l)return;var id=l.getAttribute('data-id');done[id]=ev.target.checked;save();l.classList.toggle('done',!!done[id]);});
</script></body></html>"""


def cmd_dashboard(a):
    src = Path(a.report).expanduser()
    if not src.exists() and (REPORTS / src.name).exists():
        src = REPORTS / src.name
    try:
        R = json.loads(src.read_text(encoding="utf-8"))
    except Exception as ex:
        die(f"report.json is not valid JSON: {ex}")
    M = Model(a.mask or R.get("mask", False), refresh=False)
    for s in R.get("sections", []):
        for it in s.get("items", []):
            ref = it.get("reply_to") or it.get("chat")
            # recording mode never puts a real number in the page; a demo card may set "phone" itself
            it["_phone"] = ("" if M.mask else (M.ref_phone(ref) if ref else "")) or re.sub(r"\D", "", str(it.get("phone", "")))
            if M.mask:
                c = M.chats.get(it.get("chat") or "")
                if c and it.get("name") and c["kind"] == "private":
                    it["name"] = M.chat_name(c)
                for f in ("summary", "why", "draft", "text", "name", "meta"):
                    if it.get(f):
                        it[f] = M.scrub(it[f])
    R["mask"] = M.mask
    data = json.dumps(R, ensure_ascii=False).replace("</", "<\\/")
    out = REPORTS / (src.stem + ".html")
    REPORTS.mkdir(parents=True, exist_ok=True)
    out.write_text(DASH.replace("__TITLE__", html.escape(R.get("title", "wa-radar"))).replace("__DATA__", data), encoding="utf-8")
    M.save_aliases()
    say(f"dashboard -> {out}")
    if a.open:
        if sys.platform == "darwin":
            subprocess.run(["open", str(out)])
        elif os.name == "nt":
            os.startfile(str(out))  # noqa


# ---------------------------------------------------------------- cli
def main():
    p = argparse.ArgumentParser(prog="wr", description="WA Radar: WhatsApp, read-only, for Claude")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name)
        sp.add_argument("--mask", action="store_true", help="recording mode: hide names and numbers")
        sp.set_defaults(fn=fn)
        for k, v in kw.items():
            sp.add_argument("--" + k, **v)
        return sp

    add("check", cmd_check)
    add("snapshot", cmd_snapshot)
    add("waiting", cmd_waiting, days={"type": int, "default": 60}, limit={"type": int, "default": 60})
    add("leads", cmd_leads, days={"type": int, "default": 120}, limit={"type": int, "default": 60}, kw={"default": ""})
    add("promises", cmd_promises, days={"type": int, "default": 90}, limit={"type": int, "default": 60})
    add("asks", cmd_asks, days={"type": int, "default": 60}, limit={"type": int, "default": 80}, kw={"default": ""})
    add("cold", cmd_cold, min={"type": int, "default": 40}, silent={"type": int, "default": 60}, limit={"type": int, "default": 40})
    add("brief", cmd_brief, hours={"type": int, "default": 24}, limit={"type": int, "default": 60}, kw={"default": ""}, me={"default": ""})
    sp = add("export", cmd_export, days={"type": int, "default": 30}, chat={"default": ""})
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--groups", action="store_true")
    sp = add("voice", cmd_voice, days={"type": int, "default": 14}, limit={"type": int, "default": 60})
    sp.add_argument("--groups", action="store_true")
    add("voice-setup", cmd_voice_setup)
    sp = add("import", cmd_import, me={"default": ""})
    sp.add_argument("file")
    sp = add("dashboard", cmd_dashboard)
    sp.add_argument("report")
    sp.add_argument("--open", action="store_true")
    add("clean", cmd_clean)
    a = p.parse_args()
    HOME.mkdir(parents=True, exist_ok=True)
    a.fn(a)


if __name__ == "__main__":
    main()

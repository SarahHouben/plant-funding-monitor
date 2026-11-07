"""Remove personal contact details from text before it is stored or fingerprinted.

The database is committed to a public repository, so it must not hold
people's names, email addresses or phone numbers. Functional contacts stay:
team and unit mailboxes such as info@ or foerderung@, and general hotlines.

The rules are heuristics and lean towards removing:
- An email address whose local part looks like a person's name (anna.beispiel@,
  a.beispiel@, anna_beispiel@), including obfuscated forms such as
  "a.beispiel(at)example.org" or "a.beispiel(bei)example.org". A single word
  (info@, mueller@) can't be told apart from a mailbox, so it stays.
- A contact label (Ansprechpartner/in, Kontakt, Contact person, Veranstalter,
  ...) followed by a name: the rest of that line goes, and phone numbers in
  the next few lines.
- A contact label on its own line, such as a "Kontakt" heading: in the lines
  below it, lines starting with a name, phone numbers and any address that
  isn't a mailbox (so also mueller@) go.
- A name with a phone number shortly after it goes, number and all; on a line
  with a titled name (Dr., Prof., Frau ...), every phone number goes. Hotlines
  and switchboards stay.

scrub() is used by the fetch step and is meant for call pages too.
"""

import re

# Parts of an email's local part that mark a functional mailbox.
FUNCTIONAL_PARTS = {
    "info", "infos", "kontakt", "contact", "contacts", "office", "buero", "büro", "mail", "email",
    "post", "poststelle", "service", "support", "help", "helpdesk", "hotline", "team", "presse",
    "press", "media", "grants", "grant", "funding", "foerderung", "förderung", "foerderberatung",
    "calls", "call", "programm", "programme", "program", "antrag", "antraege", "bewerbung",
    "application", "applications", "fellowships", "fellowship", "stipendien", "awards", "prize",
    "preis", "ausschreibung", "ausschreibungen", "projekt", "projekte", "project", "projects",
    "referat", "abteilung", "verwaltung", "sekretariat", "secretariat", "koordination",
    "coordination", "admin", "webmaster", "noreply", "no", "reply", "newsletter",
    "datenschutz", "privacy", "events", "veranstaltungen", "international", "research",
    "forschung", "science", "wissenschaft", "network", "netzwerk", "secretary", "general",
}
AT = r"(?:@|\s?[\(\[\{]\s?(?:at|bei|ät)\s?[\)\]\}]\s?)"
DOT = r"(?:\.|\s?[\(\[]\s?(?:dot|punkt)\s?[\)\]]\s?)"
EMAIL = re.compile(rf"(?<![\w.\\-])(?P<local>[A-Za-z0-9][A-Za-z0-9._%+\\-]*){AT}"
                   rf"(?P<domain>[A-Za-z0-9-]+(?:{DOT}[A-Za-z0-9-]+)+)", re.I)
MAILTO_LINK = re.compile(r"\[[^\]\n]*\]\(mailto:(?P<address>[^)\s]+)\)", re.I)
EMAIL_REMOVED = "[email removed]"

TITLE = (r"(?:(?:Dr|Prof|Dipl|PD|Privatdozent(?:in)?|Professor(?:in)?|Herr|Frau|Mr|Ms|Mrs)\.?"
         r"(?:-?\s?(?:rer\.\s?nat|med|phil|Ing|Biol)\.)*\s+)")
NAME = r"[A-ZÄÖÜ][a-zäöüßéèáó]+(?:-[A-ZÄÖÜ][a-zäöüßéèáó]+)?"
PERSON = (rf"(?:{TITLE}+{NAME}(?:\s+{NAME}){{0,2}}"
          rf"|{NAME}(?:\s+(?:von|van|de|zu|der))?\s+{NAME}(?:\s+{NAME})?)")
# Words that make a "name" an organisation or unit instead.
ORGANISATION = re.compile(
    r"\b(?:Office|Team|Referat|Abteilung|Geschäftsstelle|Projektträger|Programm|Programme|Program|"
    r"Secretariat|Sekretariat|Unit|Department|Service|Hotline|Helpdesk|Stiftung|Foundation|Agentur|"
    r"Agency|Institut|Institute|Gesellschaft|Ministerium|Ministry|Bundesanstalt|Fachagentur|Zentrum|"
    r"Center|Centre|Support|Desk|Beratung|Koordination|Coordination|Verwaltung|Kontaktstelle|"
    r"Contact Point|Informationen|Information|GmbH|e\.V)\b")
LABEL = (r"(?:Ansprechpartner(?:/-?in|\*in|:in|innen|in)?|Ansprechperson(?:en)?|"
         r"Kontaktperson(?:en)?|Kontakt|Contact persons?|Contact|"
         r"Veranstalter(?:/-?in|\*in|:in|innen|in)?|Organisator(?:/-?in|in)?|Organi[sz]ers?|"
         r"Koordinator(?:/-?in|in)?|Coordinators?|Sprecher(?:/-?in|in)?|Spokesperson)")
LABEL_THEN_PERSON = re.compile(rf"\b{LABEL}\s*:?\s*(?P<person>{PERSON})")
BARE_LABEL = re.compile(rf"^[\s#*_>-]*{LABEL}[\s*_:]*$", re.I)
# No dots, so dates such as 01.12.2026 never look like phone numbers.
PHONE = re.compile(r"(?:(?:Tel(?:efon)?|Phone|Fon|Fax|Mobil(?:e)?|T|F)\.?\s*:?\s*)?"
                   r"\(?(?:\+|00|0)\d[\d\s/()-]{5,}\d")
PERSON_RE = re.compile(rf"(?<![\w.]){PERSON}")
TITLED_PERSON = re.compile(rf"(?<![\w.]){TITLE}+{NAME}")
# How far before a phone number a name may end and still count as its owner.
NAME_TO_PHONE = 40
HOTLINE = re.compile(r"hotline|servicetelefon|zentrale|switchboard|helpdesk|service desk|"
                     r"general enquiries|allgemeine", re.I)
HEADING = re.compile(r"^\s*#{1,6}\s")
# Lines below a contact heading, and below a cut named contact, that are
# treated as contact details (names, addresses, phone numbers).
CONTACT_BLOCK_LINES = 8
AFTER_NAMED_CONTACT = 3


def scrub(text):
    """Return text without personal email addresses, named contacts or their phone numbers."""
    text = MAILTO_LINK.sub(lambda m: EMAIL_REMOVED if _is_personal(m["address"]) else m[0], text)
    text = EMAIL.sub(lambda m: EMAIL_REMOVED if _is_personal(m["local"]) else m[0], text)

    lines, block = [], 0
    for original in text.splitlines():
        line = original
        if BARE_LABEL.match(line):
            lines.append(line)
            block = CONTACT_BLOCK_LINES
            continue
        if block and HEADING.match(line):
            block = 0
        if block and line.strip():
            block -= 1
            if _starts_with_person(line):
                continue
            # In a contact block, any address that isn't a mailbox is a person's.
            line = MAILTO_LINK.sub(lambda m: m[0] if _is_functional(m["address"]) else EMAIL_REMOVED, line)
            line = EMAIL.sub(lambda m: m[0] if _is_functional(m["local"]) else EMAIL_REMOVED, line)
            if not HOTLINE.search(line):
                line = PHONE.sub("", line)
        cut = _cut_named_contact(line)
        if cut != line:
            line, block = cut, max(block, AFTER_NAMED_CONTACT)
        if not HOTLINE.search(line):
            line = _drop_names_with_phones(line)
            if TITLED_PERSON.search(line):
                line = PHONE.sub("", line)
        lines.append(line if line == original else _tidy(line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _is_personal(address):
    """Whether an email address, or just its local part, looks like a person's name."""
    local = address.split("@")[0].replace("\\", "").lower()
    parts = [p for p in re.split(r"[._-]", local) if p]
    if len(parts) < 2 or any(p in FUNCTIONAL_PARTS for p in parts):
        return False
    return all(p.isalpha() and (len(p) == 1 or re.search(r"[aeiouyäöü]", p)) for p in parts)


def _is_functional(address):
    """Whether an email address, or its local part, names a mailbox (info@, grants@ ...)."""
    local = address.split("@")[0].replace("\\", "").lower()
    return any(part in FUNCTIONAL_PARTS for part in re.split(r"[._-]", local))


def _starts_with_person(line):
    """Whether a line is a name: the name alone, or followed by a comma or a
    bracket ("Dr. Erika Musterfrau, Referentin"), but not by a colon, which
    makes it a label ("Allgemeine Fragen: ...")."""
    match = re.match(rf"^[\s*_>-]*(?P<person>{PERSON})[\s*_]*(?:$|,|\()", line)
    return (bool(match) and not ORGANISATION.search(line.split(",")[0])
            and not HOTLINE.search(line))


def _cut_named_contact(line):
    """Drop everything from a contact label followed by a person's name."""
    for match in LABEL_THEN_PERSON.finditer(line):
        if not ORGANISATION.search(match["person"]):
            return line[:match.start()]
    return line


def _drop_names_with_phones(line):
    """Drop each phone number together with the name just before it. The name
    nearest the number counts, since ordinary capitalised words ("Bei Fragen")
    can look like a name too; an organisation or unit keeps its number."""
    for phone in reversed(list(PHONE.finditer(line))):
        before = line[:phone.start()]
        names = list(PERSON_RE.finditer(before))
        if not names:
            continue
        name = names[-1]
        gap = before[name.end():]
        if len(gap) > NAME_TO_PHONE or re.search(r"\d", gap) or ORGANISATION.search(name[0]):
            continue
        end = phone.end() + (line[phone.end():phone.end() + 1] == ")")
        line = line[:name.start()] + line[end:]
    return line


def _tidy(line):
    """Clean up what removals leave behind: empty brackets, stray commas, spaces."""
    line = re.sub(r"\(\s*[,;]?\s*\)", "", line)
    line = re.sub(r"\s+([,.;:)])", r"\1", line)
    line = re.sub(r"([,;])\s*([,;.])", r"\2", line)
    line = re.sub(r"[ \t]{2,}", " ", line)
    line = line.rstrip(" ,;:-–")
    return line

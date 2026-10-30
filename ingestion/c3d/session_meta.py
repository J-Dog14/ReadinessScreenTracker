"""Parse QTM session.xml (UTF-16) for session type and subject metadata."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def _read_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-16", "utf-8-sig", "latin-1"):
        try:
            txt = raw.decode(enc)
            if "<" in txt:
                return txt
        except UnicodeDecodeError:
            continue
    raise ValueError(f"cannot decode {path}")


def _f(fields, tag):
    if fields is None:
        return None
    el = fields.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _email(fields):
    """Accept the email field spellings used by old and new QTM templates."""
    if fields is not None:
        for child in fields:
            tag = child.tag.rsplit("}", 1)[-1].lower().replace("-", "").replace("_", "")
            value = (child.text or "").strip().lower()
            if tag in ("email", "emailaddress") and "@" in value:
                return value
    return None


def parse_session_xml(path):
    txt = _read_text(path)
    txt = re.sub(r"^<\?xml[^>]*\?>", "", txt.strip())
    root = ET.fromstring(txt)
    subj = root if root.tag == "Subject" else root.find(".//Subject")
    sfields = subj.find("Fields") if subj is not None else None
    all_sess = root.findall(".//Session")
    # a folder can hold several QTM sessions (e.g. a readiness + an athletic screen);
    # prefer the Athletic Screen one for the session-level fields
    sess = next((x for x in all_sess if (x.get("Type") or "").strip().lower() == "athletic screen"),
                all_sess[0] if all_sess else None)
    xfields = sess.find("Fields") if sess is not None else None
    meas_type = {}          # 'CMJ 4' -> session type that measurement belongs to
    for x in all_sess:
        for m in x.findall(".//Measurement"):
            fn = (m.get("Filename") or "").rsplit(".", 1)[0].strip().upper()
            if fn:
                meas_type[fn] = (x.get("Type") or "").strip()
    num = lambda x: float(x) if x not in (None, "") else None
    return dict(
        session_type=sess.get("Type") if sess is not None else None,
        session_folder_xml=sess.get("Filename") if sess is not None else None,
        subject_name=_f(sfields, "Name"), subject_id=_f(sfields, "ID"),
        height_m=num(_f(xfields, "Height") or _f(sfields, "Height")),
        weight_kg=num(_f(xfields, "Weight") or _f(sfields, "Weight")),
        dob=_f(sfields, "DOB"), gender=_f(sfields, "Gender"), email=_email(sfields),
        creation_date=_f(xfields, "Creation_date") or _f(sfields, "Creation_date"),
        session_types=[(x.get("Type") or "").strip() for x in all_sess],
        measurement_session_type=meas_type,
        measurements=sorted(meas_type),
    )

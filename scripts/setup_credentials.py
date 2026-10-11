"""Give every user a username and password, and write them to the credentials/ folder.

    python -m scripts.setup_credentials           # logins for anyone who has none yet
    python -m scripts.setup_credentials --reset   # new passwords for everybody

Passwords are random. Only their hashes go into the database; the readable copies are written
to credentials/ (credentials.csv, WhoDoo_credentials.xlsx and a README), which is the one place
they can be read. That folder is listed in .gitignore: it must never be committed or shared
beyond the people it is for. People can change their password after signing in.
"""

import argparse
import asyncio
import csv
import secrets
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from sqlalchemy import select

from app.db.database import SessionLocal
from app.models.shared import Club, User
from app.services import auth_service
from app.services.auth_service import UserCredential

FOLDER = Path(__file__).resolve().parents[1] / "credentials"
COLUMNS = ("Rank", "Role", "Name", "Username", "Password", "Club", "Email")
ROLE_NAMES = {"president": "President", "treasurer": "Treasurer", "event_lead": "Event lead",
              "volunteer": "Volunteer", "member": "Member"}
UNKNOWN = "(set earlier - run with --reset for a new one)"


def new_password() -> str:
    """Three groups of four, easy to read out and type: no 0/O or 1/l/I to mix up."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def write_xlsx(path: Path, rows: list[dict]) -> None:
    """A plain .xlsx with a bold header row, written with the standard library only."""
    def column(index: int) -> str:
        return chr(ord("A") + index)

    def cell(ref: str, value, style: int = 0) -> str:
        if isinstance(value, int):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'

    lines = ['<row r="1">' + "".join(cell(f"{column(i)}1", name, 1) for i, name in enumerate(COLUMNS)) + "</row>"]
    for number, row in enumerate(rows, start=2):
        lines.append(f'<row r="{number}">' + "".join(cell(f"{column(i)}{number}", row[name]) for i, name in enumerate(COLUMNS)) + "</row>")
    widths = (8, 14, 26, 24, 22, 26, 34)
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths, start=1))
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             f'<cols>{cols}</cols><sheetData>{"".join(lines)}</sheetData></worksheet>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                               '<Default Extension="xml" ContentType="application/xml"/>'
                               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                               '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                               '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Logins" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                                      '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                         '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                         '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                         '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                         '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                         '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                         '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


async def run(reset: bool) -> tuple[int, int]:
    known = {}
    csv_path = FOLDER / "credentials.csv"
    if csv_path.exists() and not reset:
        with csv_path.open(newline="") as handle:
            known = {row["Username"]: row["Password"] for row in csv.DictReader(handle)}

    async with SessionLocal() as db:
        clubs = dict((await db.execute(select(Club.id, Club.name))).all())
        people = list(await db.scalars(select(User)))
        people.sort(key=lambda p: (clubs.get(p.club_id, ""), auth_service.rank(p.role), p.full_name))
        existing = {row.user_id: row for row in await db.scalars(select(UserCredential))}
        taken = {row.username for row in existing.values()}
        rows, made = [], 0
        for person in people:
            row = existing.get(person.id)
            if row is None or reset:
                username = row.username if row else auth_service.username_from(person.full_name, taken)
                taken.add(username)
                password = new_password()
                await auth_service.set_credentials(db, person.id, username, password)
                made += 1
            else:
                username, password = row.username, known.get(row.username, UNKNOWN)
            rows.append({"Rank": auth_service.rank(person.role), "Role": ROLE_NAMES.get(person.role, person.role),
                         "Name": person.full_name, "Username": username, "Password": password,
                         "Club": clubs.get(person.club_id, ""), "Email": person.email})
        await db.commit()

    FOLDER.mkdir(exist_ok=True)
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    write_xlsx(FOLDER / "WhoDoo_credentials.xlsx", rows)
    (FOLDER / "README.md").write_text(
        "# WhoDoo sign-in details\n\n"
        f"Written by `python -m scripts.setup_credentials` on {datetime.now():%d %b %Y, %H:%M}. {len(rows)} people.\n\n"
        "- `WhoDoo_credentials.xlsx` and `credentials.csv` hold the same list: rank, role, name, username, password.\n"
        "- Sign in at `/login`: pick the person on the tree, then enter their username and password.\n"
        "- Rank 1 is the president; power goes down from there (treasurer, event leads, members, volunteers).\n"
        "- Passwords are stored in the database only as hashes. These files are the only readable copy.\n"
        "- **Do not commit or share this folder.** It is in `.gitignore`. Anyone with a row here can sign in as that person.\n"
        "- `make credentials` adds logins for new people; `make credentials-reset` gives everybody a new password.\n"
    )
    return len(rows), made


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="give everybody a new password")
    args = parser.parse_args()
    total, made = asyncio.run(run(args.reset))
    print(f"{made} login{'s' if made != 1 else ''} created or reset, {total} people listed in {FOLDER.name}/ "
          "(WhoDoo_credentials.xlsx, credentials.csv). Passwords are not printed here.")


if __name__ == "__main__":
    main()

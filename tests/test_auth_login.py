"""Signing in with a username and password, and the club hierarchy."""

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.services import auth_service
from app.services.auth_service import UserCredential

API = "/api/v1"


@pytest.fixture(autouse=True)
def _no_lockouts():
    auth_service._failures.clear()
    yield
    auth_service._failures.clear()


@pytest.fixture
async def logins(db, world):
    """A login for three people. Passwords are made here and never leave the test."""
    secrets_ = {"president": "Tree-Top-9911", "treasurer": "Money-Bags-22", "member": "Just-A-Member-7"}
    for role, password in secrets_.items():
        person = getattr(world, role)
        await auth_service.set_credentials(db, person.id, auth_service.username_from(person.full_name, set()), password)
    await db.commit()
    return secrets_


def code(response) -> str:
    return response.json()["error"]["code"]


def test_the_hierarchy_runs_from_president_down():
    order = ["president", "treasurer", "event_lead", "member", "volunteer"]     # volunteers sit below members
    assert [auth_service.rank(role) for role in order] == [1, 2, 3, 4, 5]
    for higher, lower in zip(order, order[1:]):
        assert auth_service.outranks(higher, lower) and not auth_service.outranks(lower, higher)
    assert not auth_service.outranks("member", "member") and auth_service.rank("stranger") == 6


def test_passwords_are_hashed_with_a_salt():
    one, two = auth_service.hash_password("correct horse"), auth_service.hash_password("correct horse")
    assert one != two and "correct horse" not in one and one.startswith("scrypt$")
    assert auth_service.verify_password("correct horse", one) and not auth_service.verify_password("Correct horse", one)
    assert not auth_service.verify_password("x", "not-a-hash")
    assert auth_service.username_from("Mia Member", set()) == "mia.member"
    assert auth_service.username_from("Mia Member", {"mia.member", "mia.member2"}) == "mia.member3"
    assert auth_service.username_from("Li", set()) == "liuser"


async def test_signing_in_needs_the_right_username_and_password(client, db, world, logins):
    wrong = await client.post(f"{API}/auth/login", json={"username": "priya.president", "password": "nope"})
    unknown = await client.post(f"{API}/auth/login", json={"username": "nobody.here", "password": "nope"})
    # A wrong password and a username that does not exist look exactly the same.
    assert (wrong.status_code, code(wrong)) == (unknown.status_code, code(unknown)) == (401, "invalid_credentials")
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert get_settings().session_cookie not in wrong.cookies

    ok = await client.post(f"{API}/auth/login", json={"username": " Priya.President ", "password": logins["president"]})
    assert ok.status_code == 200 and (ok.json()["role"], ok.json()["rank"]) == ("president", 1)
    assert "password" not in ok.text.lower() and get_settings().session_cookie in ok.cookies
    assert (await client.get("/whoami")).json()["name"] == "Priya President"
    assert (await client.get("/treasurer")).status_code == 200                 # the session works on pages
    # Only a hash is stored, and nothing about the password reaches the audit log.
    stored = await db.scalar(select(UserCredential.password_hash).where(UserCredential.username == "priya.president"))
    assert logins["president"] not in stored
    assert logins["president"] not in ((await db.scalar(text("SELECT string_agg(details::text, ' ') FROM audit_logs"))) or "")

    assert (await client.get("/logout")).headers["location"] == "/login"
    assert (await client.get("/whoami")).json() == {"id": None, "name": None}
    assert (await client.get("/treasurer")).headers["location"] == "/login?next=/treasurer"


async def test_the_node_you_pick_must_be_the_account_you_sign_in_to(client, world, logins):
    mine = {"username": "mia.member", "password": logins["member"]}
    as_president = await client.post(f"{API}/auth/login", json={**mine, "user_id": str(world.president.id)})
    assert (as_president.status_code, code(as_president)) == (401, "invalid_credentials")      # valid login, wrong person
    assert (await client.post(f"{API}/auth/login", json={**mine, "user_id": str(world.member.id)})).status_code == 200
    # Power follows the role the account has, not anything the request says.
    assert (await client.get("/treasurer")).status_code == 403
    assert (await client.post(f"{API}/auth/login", json={**mine, "role": "president"})).status_code == 422
    # Someone with no login cannot be signed in to at all.
    none = await client.post(f"{API}/auth/login", json={"username": "vik.volunteer", "password": "anything", "user_id": str(world.volunteer.id)})
    assert none.status_code == 401


async def test_repeated_wrong_passwords_lock_the_username_for_a_while(client, world, logins):
    for _ in range(auth_service.MAX_FAILURES):
        assert (await client.post(f"{API}/auth/login", json={"username": "tara.treasurer", "password": "guess"})).status_code == 401
    locked = await client.post(f"{API}/auth/login", json={"username": "tara.treasurer", "password": logins["treasurer"]})
    assert (locked.status_code, code(locked)) == (429, "too_many_attempts")     # even the right one, until it clears
    assert (await client.post(f"{API}/auth/login", json={"username": "priya.president", "password": logins["president"]})).status_code == 200


async def test_people_can_change_their_own_password(client, world, auth, logins):
    url = f"{API}/auth/password"
    assert (await client.put(url, json={"current_password": "x", "new_password": "Brand-New-Pass-1"})).status_code == 401
    wrong = await client.put(url, headers=auth(world.member), json={"current_password": "not it", "new_password": "Brand-New-Pass-1"})
    assert (wrong.status_code, code(wrong)) == (401, "invalid_credentials")
    assert (await client.put(url, headers=auth(world.member), json={"current_password": logins["member"], "new_password": "short"})).status_code == 422
    changed = await client.put(url, headers=auth(world.member), json={"current_password": logins["member"], "new_password": "Brand-New-Pass-1"})
    assert changed.status_code == 200
    assert (await client.post(f"{API}/auth/login", json={"username": "mia.member", "password": logins["member"]})).status_code == 401
    assert (await client.post(f"{API}/auth/login", json={"username": "mia.member", "password": "Brand-New-Pass-1"})).status_code == 200


async def test_the_tree_marks_who_has_a_login(client, world, logins):
    page = (await client.get("/login")).text
    assert page.count("no login yet") == 4 and page.count("disabled") >= 4        # 7 people, 3 with logins
    president = page[page.index('data-name="Priya President"') - 200: page.index('data-name="Priya President"') + 200]
    assert "disabled" not in president


def test_the_credentials_sheet_is_a_readable_workbook(tmp_path):
    import zipfile
    from xml.etree import ElementTree

    from scripts.setup_credentials import COLUMNS, new_password, write_xlsx

    rows = [{"Rank": 1, "Role": "President", "Name": "A & B <C>", "Username": "a.b", "Password": new_password(),
             "Club": "Test Club", "Email": "a@b.example"}]
    path = tmp_path / "logins.xlsx"
    write_xlsx(path, rows)
    with zipfile.ZipFile(path) as book:
        assert {"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml", "xl/styles.xml"} <= set(book.namelist())
        sheet = ElementTree.fromstring(book.read("xl/worksheets/sheet1.xml"))          # well-formed, names escaped
    texts = [t.text for t in sheet.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
    assert texts[:len(COLUMNS)] == list(COLUMNS) and "A & B <C>" in texts and rows[0]["Password"] in texts
    password = new_password()
    assert len(password) == 14 and password != new_password() and not set(password) & set("0O1lI")

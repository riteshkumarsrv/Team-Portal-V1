"""
Nokia corporate directory for employee portal (Microsoft sign-in).
Maps @nokia.com email to the canonical roster name used in leave_requests / scrum assignee.
"""

from __future__ import annotations

# (corp_email_lower, directory_display_name, roster_employee_name)
# roster_employee_name must match team_roster / scrum assignee strings used in this app.
NOKIA_PORTAL_DIRECTORY: tuple[tuple[str, str, str], ...] = (
    ("jancy.mariam_jose@nokia.com", "Jancy Mariam Jose", "Jancy Mariam Jose"),
    ("jayachandra.mure@nokia.com", "Jayachandra Reddy Mure", "Jayachandra Reddy Mure"),
    ("zaiba.nousheen_khanum@nokia.com", "Zaiba Nousheen Khanum", "Zaiba Nousheen Khanum"),
    ("shruthi.k@nokia.com", "Shruthi K", "Shruthi K"),
    ("sangjukta.giri@nokia.com", "Sangjukta Giri", "Sangjukta Giri"),
    (
        "gumparlapati.penchala_sasi_kumar@nokia.com",
        "Gumparlapati Penchala Sasi Kumar",
        "Gumparlapati Penchala Sasi Kumar",
    ),
    ("shyam.katuri@nokia.com", "Shyam Bhaskar Katuri", "Shyam Bhaskar Katuri"),
    ("rashmi.k_nazare@nokia.com", "Rashmi K Nazare", "Rashmi K Nazare"),
    ("varshini.raj@nokia.com", "Varshini Raj", "Varshini Raj"),
    ("siya.chugh@nokia.com", "Siya Chugh", "Siya Chugh"),
    ("akanksha.jha@nokia.com", "Akanksha Jha", "Akanksha Jha"),
    ("ritesh.9.kumar@nokia.com", "Ritesh Kumar", "Akanksha Jha"),
    ("varshitha.s@nokia.com", "Varshitha S", "Varshitha S"),
    ("thummala.shaik_farhan@nokia.com", "Farhan Thumalla", "Farhan Thumalla"),
    ("siddhant.mandal@nokia.com", "Siddhant Mandal", "Siddhant Mandal"),
    ("harshitha.k@nokia.com", "Harshitha K", "Harshitha K"),
    ("gajendra.thakur@nokia.com", "Gajendra Singh Thakur", "Gajendra Singh Thakur"),
    ("dabbiru.seshasai@nokia.com", "Dabbiru Siva Seshasai", "Dabbiru Siva Seshasai"),
    ("ramya.ure@nokia.com", "Ramya Ure", "Ramya Ure"),
    ("bharath.krishna@nokia.com", "Bharath G Krishna", "Bharath G Krishna"),
    ("anas.p_a@nokia.com", "Anas P", "Anas P"),
    ("shaishta.anjum@nokia.com", "Shaishta Anjum", "Shaishta Anjum"),
    ("sasikumar.sampath@nokia.com", "Sasikumar Sampath", "Sasikumar Sampath"),
    ("mubarak.1.palagiri@nokia.com", "Mubarak Palagiri", "Mubarak Palagiri"),
    ("maddala.satyasai@nokia.com", "Maddala Satyasai", "Maddala Satyasai"),
    ("archit.sugha@nokia.com", "Archit Sugha", "Archit Sugha"),
    ("sumit.patra@nokia.com", "Sumit Patra", "Sumit Patra"),
)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def lookup_portal_directory(email: str) -> dict[str, str] | None:
    """Return dict with keys email, display_name, roster_name — or None if not in this team."""
    key = normalize_email(email)
    if not key.endswith("@nokia.com"):
        return None
    for em, display, roster in NOKIA_PORTAL_DIRECTORY:
        if em == key:
            return {"email": key, "display_name": display, "roster_name": roster}
    return None

#!/usr/bin/env python3
"""Fictional SOKKAN 2.x memory for the 2.x -> 3.0 migration end-to-end test.

52 notes of an imaginary bakery-café ("Atelier Brioche"), French and English, with dates
(file mtimes spread over 14 months, some `metadata.modified` in the frontmatter),
[[links]], and the defects normalize repairs (orphan update without frontmatter, name
not in kebab-case, broken YAML, file not following its name, text above the
frontmatter, missing type, link variants). Writes the notes and questions.json (the
bench: question -> expected note).

    python3 gen_corpus.py <memory_dir> <questions.json>
"""
import datetime
import json
import os
import sys
from pathlib import Path

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

# (name, description, body, question or None)
NOTES = [
    ("oven-firing", "Bread oven firing schedule and temperatures",
     "The wood oven is lit at 04:30; bread goes in at 250 C, viennoiseries at 190 C. "
     "See [[flour-supplier]] for the flour used.", "à quelle heure allume-t-on le four à bois ?"),
    ("flour-supplier", "Flour supplier: Moulin des Trois Ponts, weekly delivery",
     "T65 and T80 flours come from Moulin des Trois Ponts every Tuesday. Contract quota "
     "12 tonnes per year; alert at 90 %.", "who delivers our flour and when?"),
    ("sourdough-starter", "Sourdough starter feeding routine",
     "The levain is fed twice a day, 1:1:1 ratio, kept at 24 C. Never let it go below "
     "18 C overnight.", "how often do we feed the levain?"),
    ("coffee-roaster", "Coffee roaster partner and house blend",
     "House blend: 60 % Brazil, 40 % Ethiopia, roasted by Torréfaction Lumen every two "
     "weeks. Medium roast.", "quel torréfacteur fournit le mélange maison ?"),
    ("espresso-machine", "Espresso machine maintenance",
     "Backflush the group heads every evening; descale monthly; technician visit every "
     "six months (contract with Caffè Servizio).", "when do we descale the espresso machine?"),
    ("opening-hours", "Shop opening hours",
     "Tuesday to Saturday 06:30-19:00, Sunday 07:00-13:00, closed Monday.",
     "are we open on mondays?"),
    ("staff-rota", "Staff rota rules",
     "Rota published every Thursday for the following week; two bakers on the early "
     "shift, one barista from 06:30. Swaps go through [[team-chat]].",
     "when is the weekly staff schedule published?"),
    ("team-chat", "Team chat channel conventions",
     "All shift swaps and absences in the #equipe channel; urgent issues by phone to the "
     "shift lead.", None),
    ("allergen-policy", "Allergen labelling policy",
     "Every product card lists the 14 regulated allergens; nut products are baked on "
     "Fridays only, after a full clean.", "on which day do we bake products with nuts?"),
    ("price-list-2026", "Price list 2026",
     "Croissant 2.40, pain au chocolat 2.80, baguette 3.20, espresso 3.80, cappuccino "
     "4.90 (CHF).", "combien coûte un croissant ?"),
    ("card-terminal", "Card payment terminal",
     "The terminal is a rented device; if it freezes, unplug for 30 s. Settlement every "
     "night at 23:00.", "the card reader is frozen, what do I do?"),
    ("cash-closing", "Daily cash closing procedure",
     "Count the till at closing, two people sign the sheet, the bag goes in the safe; "
     "deposit at the bank on Mondays.", "procédure de fermeture de caisse"),
    ("cold-room", "Cold room temperature log",
     "Cold room must stay between 2 and 4 C; log readings at 07:00 and 15:00. Alarm "
     "calls the owner's phone.", "what temperature should the cold room be?"),
    ("waste-sorting", "Waste sorting and collection",
     "Organic waste collected Wednesday, cardboard Friday; unsold bread goes to the food "
     "bank ([[food-bank-partner]]).", "quand passe la collecte des déchets organiques ?"),
    ("food-bank-partner", "Food bank partner for unsold bread",
     "La Table du Quartier picks up unsold bread at 19:15 every day except Sunday.",
     "who collects the unsold bread?"),
    ("delivery-bike", "Delivery bike rules",
     "The cargo bike delivers within 3 km; battery charged overnight; helmet mandatory.",
     "what is the delivery radius of the cargo bike?"),
    ("wholesale-clients", "Wholesale clients list",
     "Hôtel du Lac (40 baguettes daily), Café Central (croissants), school canteen "
     "(Mon-Fri bread).", "quels clients achètent en gros ?"),
    ("hotel-du-lac-order", "Hôtel du Lac standing order",
     "40 baguettes and 60 mini viennoiseries every day at 06:00; invoice monthly.",
     None),
    ("instagram-account", "Instagram account and posting rhythm",
     "Post three times a week (Mon, Wed, Fri); photos taken before 08:00 for the light. "
     "Visuals approved by the owner.", "how often do we post on instagram?"),
    ("supplier-butter", "Butter supplier",
     "AOP butter from Laiterie des Alpes, delivered Monday and Thursday; 82 % fat for "
     "croissants.", "which butter do we use for croissants?"),
    ("croissant-recipe", "Croissant recipe and lamination",
     "Three single folds, rest 30 min between folds at 4 C; proof 2 h at 26 C.",
     "how many folds for the croissant dough?"),
    ("gluten-free-range", "Gluten-free range",
     "Gluten-free bread is baked off-site by a partner and sold sealed; never handled on "
     "the main bench.", None),
    ("fire-safety", "Fire safety",
     "Extinguishers checked yearly in March; fire blanket next to the fryer; evacuation "
     "point is the square in front.", "où est le point de rassemblement en cas d'incendie ?"),
    ("first-aid", "First aid kit and trained staff",
     "Kit under the counter; two staff trained (renewal every two years).", None),
    ("insurance", "Insurance contracts",
     "Business liability and equipment insurance with Assurance Mutuelle; renewal in "
     "January.", "when is the insurance renewed?"),
    ("lease", "Shop lease",
     "Lease runs until 2029, rent indexed yearly; landlord contact via the agency.",
     "jusqu'à quand court le bail ?"),
    ("accounting", "Accounting and VAT",
     "Fiduciary closes the books quarterly; VAT returns filed online at the end of each "
     "quarter.", "who handles the VAT returns?"),
    ("pos-software", "Point of sale software",
     "The till software exports sales every night at 02:00 to the accounting folder.",
     None),
    ("loyalty-card", "Loyalty card",
     "Tenth coffee free; stamps on paper cards, no app.", "how does the loyalty card work?"),
    ("seasonal-galette", "Galette des rois season",
     "Galettes sold from 2 to 31 January; pre-orders from mid-December.",
     "when do we sell galettes des rois?"),
    ("christmas-orders", "Christmas orders",
     "Bûches on pre-order only, deadline 20 December, pick-up 23-24 December.",
     "deadline for christmas log cake orders"),
    ("water-softener", "Water softener",
     "Salt refilled every two weeks; the softener protects the espresso machine and the "
     "steam oven.", None),
    ("steam-oven", "Steam oven for viennoiseries",
     "Rational combi oven, cleaning program every night; error E34 means the water "
     "supply is closed.", "the steam oven shows error E34"),
    ("electricity-contract", "Electricity contract",
     "Green electricity tariff, peak hours avoided for the oven pre-heating.", None),
    ("cleaning-plan", "Cleaning plan",
     "Daily: benches, floors, machines. Weekly: cold room, hood filters. Monthly: deep "
     "clean.", "how often are the hood filters cleaned?"),
    ("pest-control", "Pest control contract",
     "Quarterly visit by Hygiène Services; traps mapped in the cellar.", None),
    ("menu-board", "Chalk menu board conventions",
     "Prices in CHF, one line per item, allergens with numbers.", None),
    ("terrace", "Terrace permit and furniture",
     "Terrace permit April-October, 8 tables; furniture stored in the cellar in winter.",
     "how many tables on the terrace?"),
    ("music-licence", "Music licence",
     "Background music licensed through the collecting society; paid yearly.", None),
    ("wifi", "Customer wifi",
     "Guest network separate from the till; password changes monthly, written on the "
     "receipt.", "where do customers find the wifi password?"),
    ("onboarding", "New staff onboarding",
     "First week: hygiene training, allergen policy ([[allergen-policy]]), till training.",
     "what does a new employee learn in the first week?"),
    ("uniforms", "Uniforms",
     "Aprons washed by the laundry service, picked up Tuesday.", None),
    ("supplier-chocolate", "Chocolate supplier",
     "Couverture 64 % from Chocolaterie Nord, sticks for pain au chocolat.", None),
    ("customer-feedback", "Customer feedback handling",
     "Reply to reviews within 48 h, never argue publicly, offer a coffee when we were "
     "wrong.", "how do we answer negative online reviews?"),
    ("feedback-no-photos-kitchen", "No photos in the kitchen without the owner's OK",
     "Rule from the owner: recipes are not shared on social media.", None),
]

# defects (2.x reality), each written as a raw file
DEFECTS = {
    # the real note uses dashes in its file name; an update was appended to the
    # "convention" file name and created an orphan without frontmatter
    "oven-schedule.md": "---\nname: oven-schedule\ndescription: Weekly oven maintenance "
                        "schedule\nmetadata:\n  type: project\n---\n\nThe oven is swept "
                        "every Sunday and inspected by the mason each autumn.\n",
    "oven_schedule.md": "UPDATE: the mason now also checks the chimney in spring.\n",
    # name not kebab-case
    "delivery_van.md": "---\nname: Delivery Van Maintenance\ndescription: Van service "
                       "every 15000 km\nmetadata:\n  type: project\n---\n\nService the van "
                       "at the garage every 15 000 km; tyres changed in October.\n",
    # unquoted description with ": " breaks the YAML; a declared date
    "till_backup.md": "---\nname: till-backup\ndescription: Till backup: nightly at 02:00\n"
                      "metadata:\n  type: project\n  modified: '2026-02-10T08:00:00+00:00'\n"
                      "---\n\nThe till exports sales every night. See [[Harbour_Tides]].\n",
    # file does not follow its name; mentioned elsewhere as tides.md
    "tides.md": "---\nname: harbour-tides\ndescription: Harbour tide tables for the boat "
                "deliveries\nmetadata:\n  type: reference\n---\n\nFlour by boat arrives "
                "only at high tide.\n",
    # text pasted above the frontmatter
    "menu_specials.md": "MAJ: oat milk is now free.\n\n---\nname: menu-specials\n"
                        "description: Weekly specials board\nmetadata:\n  type: project\n"
                        "---\n\nOne special per week, chosen on Monday.\n",
    # missing type
    "feedback_no_espresso_after_six.md": "---\nname: feedback-no-espresso-after-six\n"
                                         "description: Never serve espresso after 18:00\n"
                                         "---\n\nRule from the owner. Check tides.md.\n",
}


def main(mem_dir: str, questions_path: str) -> None:
    d = Path(mem_dir)
    d.mkdir(parents=True, exist_ok=True)
    questions = []
    for i, (name, desc, body, q) in enumerate(NOTES):
        meta = "metadata:\n  type: {}\n".format(
            "feedback" if name.startswith("feedback") else "project")
        if i % 4 == 0:   # a quarter of the notes carry a declared date
            declared = NOW - datetime.timedelta(days=30 + 7 * i)
            meta += f"  modified: '{declared.isoformat()}'\n"
        (d / (name.replace("-", "_") + ".md")).write_text(
            f"---\nname: {name}\ndescription: {desc}\n{meta}---\n\n{body}\n",
            encoding="utf-8")
        if q:
            questions.append({"q": q, "expected": name})
    for fname, text in DEFECTS.items():
        (d / fname).write_text(text, encoding="utf-8")
    questions += [{"q": "when is the oven chimney checked?", "expected": "oven-schedule"},
                  {"q": "tide tables for boat deliveries", "expected": "harbour-tides"}]
    # mtimes: from 14 months ago to 3 weeks ago, distinct per file
    files = sorted(p for p in d.glob("*.md"))
    for i, p in enumerate(files):
        ts = (NOW - datetime.timedelta(days=21 + (420 - 21) * i / len(files),
                                       hours=i)).timestamp()
        os.utime(p, (ts, ts))
    Path(questions_path).write_text(json.dumps(questions, indent=1, ensure_ascii=False))
    print(f"{len(files)} files, {len(questions)} questions in {d}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

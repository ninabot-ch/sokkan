#!/usr/bin/env python3
"""onboarding_samples.py — the starter catalogue of positions and their first work (4.0).

A person's first sign-in asks what they do here (« your position ») and proposes a first work
with an EXAMPLE document — so that the first thing SOKKAN does for them is something of their
trade, on a document they did not have to find. Every text here is invented: no client, no
person, no address.

The real catalogue of positions (kits per trade: their templates, agents and rules) is another
lot. Its module, when present, wins: `roles_catalog.catalog(lang)` returning the same shape
(see `onboarding.roles`). This list is only the floor an instance has without it.
"""
from __future__ import annotations

# id → label, one line, the first ask, and the example document (name, text) — per language
STARTER: dict[str, dict[str, dict]] = {
    "management": {
        "fr": {"label": "Direction et gestion",
               "description": "Décider, suivre les dossiers, préparer les comptes rendus.",
               "ask": "Fais une synthèse d'une page de ce compte rendu : décisions prises, actions avec responsable et échéance, points ouverts.",
               "doc": ("Exemple - compte rendu de réunion.md", """# Réunion d'équipe du lundi (exemple)

Présents : direction, responsable administratif, responsable commercial, deux collaborateurs.

- Le chiffre du trimestre est en retrait de 6 % sur l'objectif ; la direction veut comprendre pourquoi avant fin du mois.
- Le site internet doit être mis à jour : tarifs 2027, photos de l'équipe. Personne n'a encore été désigné.
- Recrutement d'un assistant administratif : l'annonce est prête mais pas publiée, on attend la validation du budget.
- Le fournisseur informatique propose de renouveler le contrat de maintenance pour 3 ans ; décision reportée, il faut comparer deux autres offres.
- Formation sécurité incendie obligatoire : date à fixer avant le 15 du mois prochain.
- Décidé : les notes de frais passent désormais par un formulaire unique, dès le 1er du mois.
- Le responsable commercial relance les trois clients dont les factures ont plus de 60 jours.
"""),
               },
        "en": {"label": "Management",
               "description": "Decide, follow up the files, prepare the minutes.",
               "ask": "Write a one-page summary of these minutes: decisions taken, actions with owner and deadline, open points.",
               "doc": ("Example - meeting minutes.md", """# Monday team meeting (example)

Present: management, office manager, sales manager, two team members.

- Quarterly revenue is 6% below target; management wants to understand why before month end.
- The website needs an update: 2027 prices, team photos. Nobody has been named yet.
- Hiring an office assistant: the job ad is ready but not published, waiting for the budget approval.
- The IT supplier offers to renew the maintenance contract for 3 years; decision postponed, two other offers to compare.
- Mandatory fire-safety training: date to set before the 15th of next month.
- Decided: expense claims go through a single form from the 1st of the month.
- The sales manager chases the three clients whose invoices are over 60 days old.
"""),
               },
    },
    "admin_hr": {
        "fr": {"label": "Administration et RH",
               "description": "Annonces, contrats, courriers, procédures internes.",
               "ask": "Rédige l'annonce de recrutement à partir de ces notes : titre, mission, profil, ce que nous offrons. Ton clair et accueillant, une page au plus.",
               "doc": ("Exemple - notes pour une annonce.md", """# Notes pour l'annonce (exemple)

- Poste : assistant·e administratif·ve, 80 %, contrat à durée indéterminée.
- Tâches : accueil téléphonique, courrier, agenda de la direction, préparation des factures, classement.
- Outils : tableur, traitement de texte, logiciel de facturation (formation assurée).
- Profil : formation commerciale ou expérience équivalente, sens de l'organisation, discrétion.
- Langues : français courant, anglais un plus.
- Entrée en fonction : dès que possible.
- Nous offrons : horaires flexibles, une équipe de huit personnes, télétravail un jour par semaine.
"""),
               },
        "en": {"label": "Administration and HR",
               "description": "Job ads, contracts, letters, internal procedures.",
               "ask": "Write the job ad from these notes: title, role, profile, what we offer. Clear and welcoming, one page at most.",
               "doc": ("Example - notes for a job ad.md", """# Notes for the job ad (example)

- Position: office assistant, 80%, permanent contract.
- Tasks: phone reception, mail, management's calendar, preparing invoices, filing.
- Tools: spreadsheet, word processor, invoicing software (training provided).
- Profile: business training or equivalent experience, well organised, discreet.
- Languages: fluent French, English a plus.
- Start: as soon as possible.
- We offer: flexible hours, a team of eight, one day a week from home.
"""),
               },
    },
    "finance": {
        "fr": {"label": "Comptabilité et finance",
               "description": "Dépenses, factures, budgets, rapports chiffrés.",
               "ask": "Classe ces dépenses par catégorie avec le total de chacune, et signale ce qui paraît inhabituel (doublon, montant hors norme).",
               "doc": ("Exemple - dépenses du mois.md", """# Dépenses du mois (exemple, montants en CHF)

| Date | Libellé | Montant |
|---|---|---|
| 02 | Loyer bureau | 2 400.00 |
| 03 | Fournitures de bureau | 186.40 |
| 05 | Abonnement logiciel comptable | 59.00 |
| 05 | Abonnement logiciel comptable | 59.00 |
| 09 | Repas client | 142.50 |
| 12 | Électricité | 213.80 |
| 15 | Billets de train (salon professionnel) | 312.00 |
| 18 | Fournitures de bureau | 1 940.00 |
| 21 | Téléphonie et internet | 129.90 |
| 27 | Repas d'équipe | 380.00 |
"""),
               },
        "en": {"label": "Accounting and finance",
               "description": "Expenses, invoices, budgets, figures.",
               "ask": "Group these expenses by category with each total, and point out what looks unusual (duplicate, odd amount).",
               "doc": ("Example - expenses of the month.md", """# Expenses of the month (example, amounts in CHF)

| Day | Item | Amount |
|---|---|---|
| 02 | Office rent | 2,400.00 |
| 03 | Office supplies | 186.40 |
| 05 | Accounting software subscription | 59.00 |
| 05 | Accounting software subscription | 59.00 |
| 09 | Client lunch | 142.50 |
| 12 | Electricity | 213.80 |
| 15 | Train tickets (trade fair) | 312.00 |
| 18 | Office supplies | 1,940.00 |
| 21 | Phone and internet | 129.90 |
| 27 | Team dinner | 380.00 |
"""),
               },
    },
    "sales": {
        "fr": {"label": "Vente et relation client",
               "description": "Offres, relances, réponses aux clients.",
               "ask": "Propose une réponse à ce message de client : reconnaître le problème, ce que nous faisons et quand, un geste commercial raisonnable. Ton courtois, dix lignes au plus.",
               "doc": ("Exemple - message d'un client.md", """# Message reçu (exemple)

Bonjour,

J'ai commandé il y a trois semaines deux classeurs de rangement et une lampe de bureau. Les classeurs sont arrivés,
la lampe toujours pas, et personne ne répond au téléphone l'après-midi. Ce n'est pas la première fois.
J'aimerais savoir quand je recevrai la lampe, sinon je souhaite être remboursé.

Cordialement,
Un client (exemple)
"""),
               },
        "en": {"label": "Sales and customer care",
               "description": "Offers, follow-ups, answers to clients.",
               "ask": "Draft a reply to this client message: acknowledge the problem, what we do and when, a reasonable gesture. Courteous, ten lines at most.",
               "doc": ("Example - message from a client.md", """# Message received (example)

Hello,

Three weeks ago I ordered two storage binders and a desk lamp. The binders arrived, the lamp still has not,
and nobody answers the phone in the afternoon. This is not the first time.
I would like to know when I will receive the lamp, otherwise I would like a refund.

Regards,
A client (example)
"""),
               },
    },
    "communication": {
        "fr": {"label": "Communication et marketing",
               "description": "Publications, newsletters, textes du site.",
               "ask": "À partir de ces notes, écris une publication pour un réseau professionnel (120 mots au plus) et un paragraphe de newsletter.",
               "doc": ("Exemple - notes sur une nouveauté.md", """# Nouveauté à annoncer (exemple)

- Nous ouvrons un service de conseil le samedi matin, de 9 h à 12 h, sur rendez-vous.
- Pourquoi : beaucoup de clients travaillent en semaine et ne peuvent pas venir.
- Prise de rendez-vous en ligne ou par téléphone.
- Premier samedi : le premier du mois prochain.
- Ton souhaité : simple, chaleureux, sans superlatifs.
"""),
               },
        "en": {"label": "Communication and marketing",
               "description": "Posts, newsletters, website copy.",
               "ask": "From these notes, write a post for a professional network (120 words at most) and a newsletter paragraph.",
               "doc": ("Example - notes about a novelty.md", """# Novelty to announce (example)

- We open an advice desk on Saturday mornings, 9 to 12, by appointment.
- Why: many clients work during the week and cannot come.
- Appointments online or by phone.
- First Saturday: the first one of next month.
- Tone: simple, warm, no superlatives.
"""),
               },
    },
    "training": {
        "fr": {"label": "Formation et pédagogie",
               "description": "Supports de cours, quiz, fiches pratiques.",
               "ask": "Prépare un quiz de cinq questions à choix multiple sur ce support, avec la bonne réponse et une phrase d'explication pour chacune.",
               "doc": ("Exemple - support de cours.md", """# Les bases d'un mot de passe sûr (exemple)

Un mot de passe sûr est long : au moins douze caractères, idéalement une phrase de plusieurs mots.
Il est unique : un mot de passe réutilisé sur plusieurs sites tombe partout dès qu'un site est piraté.
Un gestionnaire de mots de passe retient les mots de passe à notre place ; il suffit d'en retenir un seul, solide.
La double authentification ajoute une seconde preuve (un code sur le téléphone) : un mot de passe volé ne suffit plus.
On ne communique jamais son mot de passe, ni par téléphone ni par courriel, même à quelqu'un qui se présente comme le support.
"""),
               },
        "en": {"label": "Training and teaching",
               "description": "Course material, quizzes, practical sheets.",
               "ask": "Prepare a five-question multiple-choice quiz on this material, with the right answer and a one-sentence explanation for each.",
               "doc": ("Example - course material.md", """# The basics of a safe password (example)

A safe password is long: at least twelve characters, ideally a sentence of several words.
It is unique: a password used on several sites falls everywhere as soon as one site is breached.
A password manager remembers passwords for us; we only need to remember one strong one.
Two-factor authentication adds a second proof (a code on the phone): a stolen password is no longer enough.
We never give our password away, neither by phone nor by e-mail, even to someone who says they are the support.
"""),
               },
    },
    "other": {
        "fr": {"label": "Autre poste",
               "description": "Vous ne vous retrouvez pas dans la liste : commencez par un résumé.",
               "ask": "Résume ce document en cinq points, puis propose les trois prochaines actions.",
               "doc": ("Exemple - note interne.md", """# Note interne (exemple)

Depuis le début de l'année, les demandes reçues par courriel ont doublé. Elles arrivent dans trois boîtes différentes,
certaines restent sans réponse plusieurs jours, et deux clients s'en sont plaints. Une boîte commune avec un tour de
rôle a été proposée ; une autre idée est une réponse automatique qui annonce le délai de traitement. Le sujet sera
discuté à la prochaine réunion ; chacun est invité à noter les demandes restées sans réponse cette semaine.
"""),
               },
        "en": {"label": "Another position",
               "description": "Not in the list: start with a summary.",
               "ask": "Summarise this document in five points, then suggest the next three actions.",
               "doc": ("Example - internal note.md", """# Internal note (example)

Since the start of the year, the requests received by e-mail have doubled. They arrive in three different inboxes,
some stay unanswered for days, and two clients complained. A shared inbox with a rota was suggested; another idea is
an automatic reply announcing the handling time. The topic will be discussed at the next meeting; everyone is asked to
note the requests left unanswered this week.
"""),
               },
    },
}

ORDER = ("management", "admin_hr", "finance", "sales", "communication", "training", "other")

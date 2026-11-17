#!/usr/bin/env python3
"""onboarding_samples.py — the example document of each job, for a person's first work (4.0).

A person's first sign-in asks for their job (the catalogue of job kits, `backend/roles.py`, the
one source of jobs) and proposes a first work with an EXAMPLE document — so that the first thing
SOKKAN does for them is something of their trade, on a document they did not have to find. This
file only holds those examples, keyed by job id; labels and descriptions are the catalogue's.
Every text here is invented: no client, no person, no address.
"""
from __future__ import annotations

# job id (backend/roles/*.json) → the first ask and its example document (name, text), per language;
# "" = the fallback, for a job without its own example (an operator's extra job, or none chosen)
SAMPLES: dict[str, dict[str, dict]] = {
    "executive-assistant": {
        "fr": {"ask": "Fais une synthèse d'une page de ce compte rendu : décisions prises, actions avec responsable et échéance, points ouverts.",
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
        "en": {"ask": "Write a one-page summary of these minutes: decisions taken, actions with owner and deadline, open points.",
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
    "hr": {
        "fr": {"ask": "Rédige l'annonce de recrutement à partir de ces notes : titre, mission, profil, ce que nous offrons. Ton clair et accueillant, une page au plus.",
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
        "en": {"ask": "Write the job ad from these notes: title, role, profile, what we offer. Clear and welcoming, one page at most.",
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
        "fr": {"ask": "Classe ces dépenses par catégorie avec le total de chacune, et signale ce qui paraît inhabituel (doublon, montant hors norme).",
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
        "en": {"ask": "Group these expenses by category with each total, and point out what looks unusual (duplicate, odd amount).",
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
        "fr": {"ask": "Propose une réponse à ce message de client : reconnaître le problème, ce que nous faisons et quand, un geste commercial raisonnable. Ton courtois, dix lignes au plus.",
               "doc": ("Exemple - message d'un client.md", """# Message reçu (exemple)

Bonjour,

J'ai commandé il y a trois semaines deux classeurs de rangement et une lampe de bureau. Les classeurs sont arrivés,
la lampe toujours pas, et personne ne répond au téléphone l'après-midi. Ce n'est pas la première fois.
J'aimerais savoir quand je recevrai la lampe, sinon je souhaite être remboursé.

Cordialement,
Un client (exemple)
"""),
               },
        "en": {"ask": "Draft a reply to this client message: acknowledge the problem, what we do and when, a reasonable gesture. Courteous, ten lines at most.",
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
    "marketing": {
        "fr": {"ask": "À partir de ces notes, écris une publication pour un réseau professionnel (120 mots au plus) et un paragraphe de newsletter.",
               "doc": ("Exemple - notes sur une nouveauté.md", """# Nouveauté à annoncer (exemple)

- Nous ouvrons un service de conseil le samedi matin, de 9 h à 12 h, sur rendez-vous.
- Pourquoi : beaucoup de clients travaillent en semaine et ne peuvent pas venir.
- Prise de rendez-vous en ligne ou par téléphone.
- Premier samedi : le premier du mois prochain.
- Ton souhaité : simple, chaleureux, sans superlatifs.
"""),
               },
        "en": {"ask": "From these notes, write a post for a professional network (120 words at most) and a newsletter paragraph.",
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
        "fr": {"ask": "Prépare un quiz de cinq questions à choix multiple sur ce support, avec la bonne réponse et une phrase d'explication pour chacune.",
               "doc": ("Exemple - support de cours.md", """# Les bases d'un mot de passe sûr (exemple)

Un mot de passe sûr est long : au moins douze caractères, idéalement une phrase de plusieurs mots.
Il est unique : un mot de passe réutilisé sur plusieurs sites tombe partout dès qu'un site est piraté.
Un gestionnaire de mots de passe retient les mots de passe à notre place ; il suffit d'en retenir un seul, solide.
La double authentification ajoute une seconde preuve (un code sur le téléphone) : un mot de passe volé ne suffit plus.
On ne communique jamais son mot de passe, ni par téléphone ni par courriel, même à quelqu'un qui se présente comme le support.
"""),
               },
        "en": {"ask": "Prepare a five-question multiple-choice quiz on this material, with the right answer and a one-sentence explanation for each.",
               "doc": ("Example - course material.md", """# The basics of a safe password (example)

A safe password is long: at least twelve characters, ideally a sentence of several words.
It is unique: a password used on several sites falls everywhere as soon as one site is breached.
A password manager remembers passwords for us; we only need to remember one strong one.
Two-factor authentication adds a second proof (a code on the phone): a stolen password is no longer enough.
We never give our password away, neither by phone nor by e-mail, even to someone who says they are the support.
"""),
               },
    },
    "customer-support": {
        "fr": {"ask": "Rédige la réponse à cette réclamation : reconnais le problème, explique ce que nous faisons et dans quel délai, ton courtois et précis, une demi-page au plus.",
               "doc": ("Exemple - réclamation d'un client.md", """# Réclamation reçue par courriel (exemple)

Bonjour,

J'ai commandé une chaise de bureau le 3 du mois. La livraison était annoncée sous cinq jours ouvrables ; elle est
arrivée avec douze jours de retard et l'accoudoir gauche était cassé. J'ai appelé deux fois le service client, on m'a
promis un rappel qui n'est jamais venu. Je souhaite un remplacement de la pièce ou un remboursement partiel, et une
réponse écrite cette semaine.

Un client

Notes internes : commande payée, transporteur en retard confirmé ; pièce de rechange en stock, envoi possible en 48 h ;
geste commercial autorisé jusqu'à 10 % du prix.
"""),
               },
        "en": {"ask": "Write the reply to this complaint: acknowledge the problem, explain what we do and by when, courteous and precise, half a page at most.",
               "doc": ("Example - customer complaint.md", """# Complaint received by e-mail (example)

Hello,

I ordered an office chair on the 3rd of the month. Delivery was announced within five working days; it arrived twelve
days late and the left armrest was broken. I called customer service twice and was promised a call back that never
came. I would like a replacement part or a partial refund, and a written answer this week.

A customer

Internal notes: order paid, carrier delay confirmed; spare part in stock, can ship within 48 h; goodwill gesture
allowed up to 10% of the price.
"""),
               },
    },
    "legal": {
        "fr": {"ask": "Relis ces clauses de contrat : pour chacune, ce qu'elle nous impose, le risque (faible, moyen, élevé) et une question à poser au fournisseur. Pas d'avis juridique définitif : une revue à valider.",
               "doc": ("Exemple - extrait de contrat.md", """# Contrat de maintenance informatique - extrait (exemple)

Art. 4 Durée : le contrat est conclu pour trois ans et se renouvelle tacitement pour trois ans, sauf résiliation
par écrit six mois avant l'échéance.
Art. 7 Prix : le prestataire peut adapter ses tarifs une fois par an, sans plafond, avec un préavis de 30 jours.
Art. 9 Responsabilité : la responsabilité du prestataire est limitée au montant d'un mois de redevance.
Art. 11 Données : le prestataire peut faire appel à des sous-traitants, y compris hors de Suisse, sans en informer le client.
Art. 14 For : le for est au siège du prestataire.
"""),
               },
        "en": {"ask": "Review these contract clauses: for each, what it commits us to, the risk (low, medium, high) and one question for the supplier. No final legal opinion: a review to be checked.",
               "doc": ("Example - contract excerpt.md", """# IT maintenance contract - excerpt (example)

Art. 4 Term: the contract runs for three years and renews tacitly for three years unless terminated in writing six
months before expiry.
Art. 7 Price: the supplier may adjust its rates once a year, without a cap, with 30 days' notice.
Art. 9 Liability: the supplier's liability is limited to one month of fees.
Art. 11 Data: the supplier may use subcontractors, including outside Switzerland, without informing the client.
Art. 14 Jurisdiction: the courts of the supplier's seat.
"""),
               },
    },
    "project-manager": {
        "fr": {"ask": "Fais le point d'avancement du projet à partir de ces notes : état global (vert, orange, rouge), ce qui est fait, ce qui glisse, les risques et les décisions à demander au comité.",
               "doc": ("Exemple - notes de suivi de projet.md", """# Projet de nouveau site internet - notes de la semaine (exemple)

- Maquettes validées par la direction mardi ; deux pages encore à revoir (tarifs, contact).
- Rédaction des textes : 6 pages sur 10 livrées ; la personne qui rédige est absente la semaine prochaine.
- Développement : démarré, l'agence annonce une semaine de retard sur la page de réservation.
- Budget : 70 % consommé pour environ 55 % du travail fait.
- Mise en ligne prévue le 1er du mois prochain ; l'agence propose de la repousser de deux semaines.
- Question ouverte : faut-il traduire le site en allemand dès la mise en ligne ?
"""),
               },
        "en": {"ask": "Write the project status from these notes: overall state (green, amber, red), what is done, what slips, the risks and the decisions to ask the steering committee for.",
               "doc": ("Example - project follow-up notes.md", """# New website project - notes of the week (example)

- Mock-ups approved by management on Tuesday; two pages still to rework (prices, contact).
- Copywriting: 6 pages out of 10 delivered; the writer is away next week.
- Development: started, the agency announces a one-week delay on the booking page.
- Budget: 70% spent for about 55% of the work done.
- Go-live planned on the 1st of next month; the agency suggests postponing it by two weeks.
- Open question: should the site be translated into German at go-live?
"""),
               },
    },
    "": {
        "fr": {"ask": "Résume ce document en cinq points, puis propose les trois prochaines actions.",
               "doc": ("Exemple - note interne.md", """# Note interne (exemple)

Depuis le début de l'année, les demandes reçues par courriel ont doublé. Elles arrivent dans trois boîtes différentes,
certaines restent sans réponse plusieurs jours, et deux clients s'en sont plaints. Une boîte commune avec un tour de
rôle a été proposée ; une autre idée est une réponse automatique qui annonce le délai de traitement. Le sujet sera
discuté à la prochaine réunion ; chacun est invité à noter les demandes restées sans réponse cette semaine.
"""),
               },
        "en": {"ask": "Summarise this document in five points, then suggest the next three actions.",
               "doc": ("Example - internal note.md", """# Internal note (example)

Since the start of the year, the requests received by e-mail have doubled. They arrive in three different inboxes,
some stay unanswered for days, and two clients complained. A shared inbox with a rota was suggested; another idea is
an automatic reply announcing the handling time. The topic will be discussed at the next meeting; everyone is asked to
note the requests left unanswered this week.
"""),
               },
    },
}


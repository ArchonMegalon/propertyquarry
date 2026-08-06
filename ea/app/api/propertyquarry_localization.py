from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Awaitable, Callable

from starlette.types import Message, Receive, Scope, Send


PROPERTYQUARRY_LOCALE_COOKIE = "pq_locale"
PROPERTYQUARRY_PUBLIC_LOCALES = ("en", "de-AT", "de-DE", "es-CR")
PROPERTYQUARRY_PSEUDO_LOCALE = "qps-ploc"
PROPERTYQUARRY_MAX_LOCALIZED_HTML_BYTES = 2 * 1024 * 1024
PROPERTYQUARRY_PUBLIC_ORIGIN = "https://propertyquarry.com"
PROPERTYQUARRY_REQUIRED_CUSTOMER_ROUTE_TEMPLATES = (
    "/",
    "/pricing",
    "/security",
    "/support",
    "/privacy",
    "/terms",
    "/cookies",
    "/subprocessors",
    "/refunds",
    "/disclaimers",
    "/imprint",
    "/integrations",
    "/docs",
    "/guides/wohnung-kaufen-wien-checkliste",
    "/markets/vienna",
    "/sign-in",
    "/register",
    "/app/search",
    "/app/properties",
    "/app/shortlist",
    "/app/agents",
    "/app/alerts",
    "/app/research",
    "/app/account",
    "/app/billing",
    "/app/support",
    "/app/settings/google",
    "/app/settings/access",
    "/app/settings/usage",
    "/app/settings/support",
    "/app/settings/trust",
    "/app/settings/invitations",
    "/app/settings/outcomes",
    "/app/settings/plan",
    "/app/properties/packets",
    "/app/properties/notifications/preview",
    "/app/research/{candidate_ref}",
    "/app/shortlist/run/{run_id}",
    "/tours/{slug}",
)

_LOCALE_LABELS = {
    "en": "English",
    "de-AT": "Deutsch (Österreich)",
    "de-DE": "Deutsch (Deutschland)",
    "es-CR": "Español (Costa Rica)",
}

_LOCALE_ALIASES = {
    "en": "en",
    "en-gb": "en",
    "en-us": "en",
    "de": "de-DE",
    "de-at": "de-AT",
    "de-de": "de-DE",
    "es": "es-CR",
    "es-cr": "es-CR",
}

_DE_AT = {
    "PropertyQuarry Search": "PropertyQuarry Suche",
    "PropertyQuarry Shortlist": "PropertyQuarry Merkliste",
    "PropertyQuarry Research": "PropertyQuarry Recherche",
    "Skip to content": "Zum Inhalt springen",
    "Product": "Produkt",
    "How it works": "So funktioniert es",
    "Pricing": "Preise",
    "Sign in": "Anmelden",
    "Open search": "Suche öffnen",
    "Email sign-in": "Mit E-Mail anmelden",
    "Your privacy choice": "Ihre Datenschutzauswahl",
    "Optional analytics help us understand which public pages are useful. They stay off unless you allow them. Essential security and preference cookies still work.": (
        "Optionale Analysen helfen uns zu verstehen, welche öffentlichen Seiten nützlich sind. "
        "Sie bleiben deaktiviert, bis Sie zustimmen. Notwendige Sicherheits- und "
        "Einstellungscookies funktionieren weiterhin."
    ),
    "Review cookie settings": "Cookie-Einstellungen prüfen",
    "Reject optional analytics": "Optionale Analysen ablehnen",
    "Allow analytics": "Analysen erlauben",
    "Search once. See the right homes. Decide faster.": (
        "Einmal suchen. Die richtigen Immobilien sehen. Schneller entscheiden."
    ),
    "One brief, matching homes, and the details that matter before a viewing.": (
        "Ein Suchprofil, passende Immobilien und die entscheidenden Details vor der Besichtigung."
    ),
    "Brief": "Suchprofil",
    "Decide": "Entscheiden",
    "Example shortlist": "Beispiel-Merkliste",
    "Tap any row to open the example.": "Öffnen Sie ein Beispiel mit einem Klick.",
    "Example": "Beispiel",
    "Sample homes": "Beispielimmobilien",
    "3 examples": "3 Beispiele",
    "Example home with a 3D tour and walkthrough.": (
        "Beispielimmobilie mit 3D-Tour und Rundgang."
    ),
    "Quiet layout near transit": "Ruhiger Grundriss mit guter Anbindung",
    "Good fit. Parking still unclear.": "Gute Eignung. Parkplatz noch ungeklärt.",
    "Strong price, open questions": "Attraktiver Preis, offene Fragen",
    "Strong price. Check the details.": "Attraktiver Preis. Details prüfen.",
    "Must-haves stay clear": "Unverzichtbares bleibt klar",
    "Area, mode, and must-haves can exclude.": (
        "Gebiet, Angebotsart und Muss-Kriterien können Ergebnisse ausschließen."
    ),
    "Preferences shape fit": "Präferenzen bestimmen die Eignung",
    "Schools, errands, noise, and commute adjust order.": (
        "Schulen, Alltagswege, Lärm und Pendelzeit beeinflussen die Reihenfolge."
    ),
    "Details stay together": "Alle Details an einem Ort",
    "Costs, floorplans, tours, and open questions stay with the home.": (
        "Kosten, Grundrisse, Touren und offene Fragen bleiben bei der Immobilie."
    ),
    "Property onboarding": "Immobiliensuche einrichten",
    "Set up your property search.": "Richten Sie Ihre Immobiliensuche ein.",
    "Create an account, then start with your brief.": (
        "Erstellen Sie ein Konto und beginnen Sie mit Ihrem Suchprofil."
    ),
    "Email": "E-Mail",
    "Create your account.": "Erstellen Sie Ihr Konto.",
    "Send verification code": "Bestätigungscode senden",
    "Privacy": "Datenschutz",
    "Terms": "Nutzungsbedingungen",
    "Support": "Hilfe",
    "Imprint": "Impressum",
    "Cookie settings": "Cookie-Einstellungen",
    "Subprocessors": "Unterauftragsverarbeiter",
    "Refunds": "Rückerstattungen",
    "Disclaimers": "Haftungsausschlüsse",
    "Ready": "Bereit",
    "1. Email": "1. E-Mail",
    "2. Details": "2. Details",
    "3. Access": "3. Zugriff",
    "4. Ready": "4. Bereit",
    "Step 1": "Schritt 1",
    "Try again": "Erneut versuchen",
    "Search": "Suche",
    "Shortlist": "Merkliste",
    "Research": "Recherche",
    "Account": "Konto",
    "Research desk": "Recherchebereich",
    "PropertyQuarry sections": "PropertyQuarry Bereiche",
    "PropertyQuarry public home": "Öffentliche PropertyQuarry Startseite",
    "Dark mode": "Dunkelmodus",
    "Browser alerts": "Browser-Benachrichtigungen",
    "Browser alerts on": "Browser-Benachrichtigungen an",
    "Browser alerts off": "Browser-Benachrichtigungen aus",
    "Light mode": "Hellmodus",
    "Stop": "Stoppen",
    "Searching": "Suche läuft",
    "Results ready": "Ergebnisse bereit",
    "Paused": "Pausiert",
    "estimating": "wird geschätzt",
    "Waiting for homes.": "Warten auf Immobilien.",
    "Looking for homes": "Suche nach Immobilien",
    "Preparing search": "Suche wird vorbereitet",
    "Preparing search.": "Suche wird vorbereitet.",
    "Homes": "Immobilien",
    "To review": "Zu prüfen",
    "Checks left": "Offene Prüfungen",
    "Checked": "Geprüft",
    "Sites": "Portale",
    "Updates": "Aktualisierungen",
    "Search totals": "Suchsummen",
    "Search progress": "Suchfortschritt",
    "Homes found": "Immobilien gefunden",
    "Homes checked": "Geprüft",
    "Checking the saved search.": "Gespeicherte Suche wird geprüft.",
    "Checking the latest search status.": "Aktueller Suchstatus wird geprüft.",
    "Saved defaults": "Gespeicherte Vorgaben",
    "Access": "Zugriff",
    "Billing": "Abrechnung",
    "Log out": "Abmelden",
    "Account navigation": "Kontonavigation",
    "Launch search": "Suche starten",
    "Search brief": "Suchprofil",
    "Adjust search": "Suche anpassen",
    "Back": "Zurück",
    "Next": "Weiter",
    "Review": "Prüfen",
    "Market": "Markt",
    "Where": "Wo",
    "What": "Was",
    "Strategy": "Strategie",
    "Guardrails": "Leitplanken",
    "Details": "Details",
    "Reachability": "Erreichbarkeit",
    "Numbers": "Kennzahlen",
    "Research depth": "Recherchetiefe",
    "Providers": "Portale",
    "Market, listing mode, and budget.": "Markt, Angebotsart und Budget.",
    "Country, market, and target areas.": "Land, Markt und Zielgebiete.",
    "Type, budget, size, move-in.": "Art, Budget, Größe und Einzug.",
    "Target areas, spillover distance, and investment guardrails.": "Zielgebiete, Suchradius und Anlageleitplanken.",
    "Execution risk, legal context, and which weak listings should be excluded.": "Umsetzungsrisiko, Rechtslage und auszuschließende schwache Angebote.",
    "Preferences, family setup, errands, and local-fit rules.": "Vorlieben, Familie, Alltagswege und Regeln zur Lageeignung.",
    "How much supporting detail the shortlist needs.": "Wie viele Nachweise die Merkliste benötigt.",
    "Destinations, travel modes, and time limits that change fit.": "Ziele, Verkehrsmittel und Zeitlimits, die die Eignung beeinflussen.",
    "Return, risk, and external data depth.": "Rendite, Risiko und Tiefe externer Daten.",
    "Risk, supply, and how much detail each strong match should carry.": "Risiko, Angebot und Detailtiefe für jeden starken Treffer.",
    "Choose providers.": "Portale auswählen.",
    "Launching...": "Suche wird gestartet…",
    "Preparing search...": "Suche wird vorbereitet…",
    "Starting search...": "Suche wird vorbereitet…",
    "Launching search...": "Suche wird gestartet…",
    "Opening results...": "Ergebnisse werden geöffnet…",
    "Opening active search...": "Laufende Suche wird geöffnet…",
    "Search started. Opening results...": "Suche gestartet. Ergebnisse werden geöffnet…",
    "Saving...": "Wird gespeichert…",
    "Saved.": "Gespeichert.",
    "Save search": "Suche speichern",
    "Save changes": "Änderungen speichern",
    "Save as new": "Als neu speichern",
    "Reset": "Zurücksetzen",
    "Search flow": "Suchablauf",
    "Find a home": "Wohnung oder Haus finden",
    "Find an investment": "Anlageimmobilie finden",
    "What are you looking for?": "Wonach suchen Sie?",
    "Country": "Land",
    "Austria": "Österreich",
    "Germany": "Deutschland",
    "Costa Rica": "Costa Rica",
    "Rent": "Mieten",
    "Buy": "Kaufen",
    "Search mode": "Suchmodus",
    "Property type": "Immobilienart",
    "Off": "Aus",
    "Investment research": "Anlagerecherche",
    "Investment research on buy listings": "Anlagerecherche bei Kaufangeboten",
    "Investment strategy": "Anlagestrategie",
    "Best overall opportunity": "Beste Gesamtchance",
    "Cash flow": "Cashflow",
    "Appreciation": "Wertsteigerung",
    "Undervalued": "Unterbewertet",
    "Low risk": "Geringes Risiko",
    "State or metro area": "Bundesland oder Ballungsraum",
    "Target areas": "Zielgebiete",
    "Map": "Karte",
    "List": "Liste",
    "All areas": "Alle Gebiete",
    "Clear": "Leeren",
    "Done": "Fertig",
    "Zoom": "Zoom",
    "Meters": "Meter",
    "Kilometers": "Kilometer",
    "Unit": "Einheit",
    "Add areas manually": "Gebiete manuell hinzufügen",
    "Search sources": "Suchquellen",
    "All sites": "Alle Portale",
    "Result mode": "Ergebnismodus",
    "Strict shortlist": "Strenge Merkliste",
    "Discovery pass": "Breite Suche",
    "Load": "Laden",
    "Save": "Speichern",
    "Saved": "Gespeichert",
    "What matters": "Was wichtig ist",
    "Home": "Wohnen",
    "Daily life": "Alltag",
    "Checks": "Prüfpunkte",
    "Neutral": "Neutral",
    "Avoid": "Vermeiden",
    "Nice to have": "Wünschenswert",
    "Strong wish": "Starker Wunsch",
    "Must have": "Unverzichtbar",
    "saved homes": "gespeicherte Immobilien",
    "matching homes": "passende Immobilien",
    "matching opportunities": "passende Anlagechancen",
    "Saved pages": "Gespeicherte Seiten",
    "Edit search": "Suche bearbeiten",
    "At a glance": "Auf einen Blick",
    "Next step": "Nächster Schritt",
    "Premium next step": "Premium-Nächster Schritt",
    "Best so far": "Bisher am besten",
    "Diorama preview in progress": "Diorama-Vorschau wird erstellt",
    "Fit": "Eignung",
    "Monthly total": "Monatlich gesamt",
    "It is meaningfully cheaper.": "Deutlich günstiger.",
    "Tour not available yet.": "Noch keine Tour verfügbar.",
    "Open listing": "Angebot öffnen",
    "Open the property page for more context.": "Öffnen Sie die Immobilienseite für weitere Details.",
    "No price published": "Kein Preis veröffentlicht",
    "3D tour available": "3D-Tour verfügbar",
    "3D tour queued": "3D-Tour eingeplant",
    "Request 3D tour": "3D-Tour anfordern",
    "Preparing 3D tour.": "3D-Tour wird vorbereitet.",
    "Walkthrough available": "Rundgang verfügbar",
    "Walkthrough queued": "Rundgang eingeplant",
    "Request walkthrough": "Rundgang anfordern",
    "Request when you want a guided walkthrough.": "Fordern Sie bei Bedarf einen geführten Rundgang an.",
    "More": "Mehr",
    "Selected property": "Ausgewählte Immobilie",
    "Provider": "Anbieter",
    "Operating costs not listed": "Betriebskosten nicht angegeben",
    "Media preview not available.": "Medienvorschau noch nicht verfügbar.",
    "The listing is still available, and your saved property details are unchanged.": "Das Angebot ist weiterhin verfügbar; Ihre gespeicherten Immobiliendetails bleiben unverändert.",
    "Best part": "Größter Vorteil",
    "Keep in mind": "Zu beachten",
    "Price level": "Preisniveau",
    "Return": "Rendite",
    "Not available yet.": "Noch nicht verfügbar.",
    "This home matches the current brief.": "Diese Immobilie entspricht dem aktuellen Suchprofil.",
    "This opportunity matches the current brief.": "Diese Anlagechance entspricht dem aktuellen Suchprofil.",
    "No major drawback yet.": "Noch kein wesentlicher Nachteil erkannt.",
    "Next question": "Nächste Frage",
    "Next to check": "Als Nächstes prüfen",
    "Financial picture": "Finanzübersicht",
    "Market data": "Marktdaten",
    "Tune search": "Suche anpassen",
    "Hide search settings": "Sucheinstellungen ausblenden",
    "Viewing requested": "Besichtigung angefragt",
    "Documents requested": "Unterlagen angefragt",
    "Offer candidate": "Kaufangebot erwägen",
    "Archived": "Archiviert",
    "Property decision reaction": "Reaktion auf die Immobilie",
    "Hide": "Ausblenden",
    "Choose one so the next search gets better.": "Wählen Sie eine Option, damit die nächste Suche besser wird.",
    "What worked": "Was überzeugt hat",
    "Loading the next things to check.": "Die nächsten Prüfpunkte werden geladen.",
    "What should change next time?": "Was soll sich bei der nächsten Suche ändern?",
    "Save decision": "Entscheidung speichern",
    "Only pay for the next move when the finished search or selected home needs it.": "Zahlen Sie nur für den nächsten Schritt, wenn die abgeschlossene Suche oder die ausgewählte Immobilie ihn benötigt.",
    "Upgrade": "Upgrade",
    "Premium market report": "Premium-Marktbericht",
    "Offer": "Angebot",
    "Open checkout": "Zur Kasse",
    "Concierge shortlist refresh": "Concierge-Aktualisierung der Auswahlliste",
    "EUR 99 | Use a paid rerun only when this shortlist needs a sharper second pass.": "EUR 99 | Nutzen Sie einen bezahlten Neulauf nur, wenn diese Auswahlliste einen präziseren zweiten Durchgang benötigt.",
    "Agent-ready export": "Maklerfertiger Export",
    "EUR 19 | Export the selected result only when a tighter handoff would save real time.": "EUR 19 | Exportieren Sie das ausgewählte Ergebnis nur, wenn eine kompaktere Übergabe wirklich Zeit spart.",
    "Share this home": "Diese Immobilie teilen",
    "Share results": "Ergebnisse teilen",
    "Best points": "Die wichtigsten Vorteile",
    "No match notes yet.": "Noch keine Hinweise zur Eignung.",
    "Facts": "Fakten",
    "Signed in": "Angemeldet",
    "Current property": "Aktuelle Immobilie",
    "Living area": "Wohnfläche",
    "Rooms": "Zimmer",
    "Costs": "Kosten",
    "Features": "Merkmale",
    "Why it works": "Warum sie passt",
    "Match": "Übereinstimmung",
    "Nothing else yet.": "Noch keine weiteren Hinweise.",
    "Around it": "Umgebung",
    "About the home": "Über die Immobilie",
    "Energy": "Energie",
    "Timeline": "Verlauf",
    "No tracked question yet": "Noch keine Frage vorgemerkt",
    "Use Clippy or Ask agent next to start a tracked follow-up.": "Nutzen Sie als Nächstes Clippy oder „Agent fragen“, um eine vorgemerkte Nachfrage zu starten.",
    "Found on list": "In der Liste gefunden",
    "360 state": "360°-Status",
    "360 gap": "360°-Lücke",
    "Unavailable | Tour not available yet.": "Nicht verfügbar | Noch keine Tour verfügbar.",
    "Property page ready": "Immobilienseite bereit",
    "The property page is ready for household or advisor follow-up.": "Die Immobilienseite ist für die weitere Prüfung im Haushalt oder mit Beratung bereit.",
    "Things to watch": "Zu beachten",
    "Visuals": "Visualisierungen",
    "Request 3D tour or walkthrough": "3D-Tour oder Rundgang anfordern",
    "Visuals not published yet": "Visualisierungen noch nicht veröffentlicht",
    "Visuals not available yet": "Visualisierungen noch nicht verfügbar",
    "Area": "Gebiet",
    "Area map": "Gebietskarte",
    "Map and nearby context.": "Karte und Informationen zur Umgebung.",
    "Switch layers.": "Ebenen wechseln.",
    "Showing available layers.": "Verfügbare Ebenen werden angezeigt.",
    "Floorplan": "Grundriss",
    "medium": "mittel",
    "Unavailable": "Nicht verfügbar",
    "Open score guide": "Bewertungsleitfaden öffnen",
    "Start search": "Suche starten",
    "Property research": "Immobilienrecherche",
    "Research updated": "Recherche aktualisiert",
    "Research packet": "Recherchepaket",
    "Back to shortlist": "Zurück zur Merkliste",
    "Open property": "Immobilie öffnen",
    "Evidence": "Nachweise",
    "Decision": "Entscheidung",
    "Reactions": "Reaktionen",
    "What changed": "Was sich geändert hat",
    "Follow-up": "Nachfassen",
    "Yes": "Ja",
    "No": "Nein",
    "Maybe": "Vielleicht",
    "Request documents": "Unterlagen anfordern",
    "View shortlist": "Merkliste ansehen",
    "You’re offline.": "Sie sind offline.",
    "Keep this page open. Reconnect, then try again. Your saved work is unchanged.": (
        "Lassen Sie diese Seite geöffnet. Stellen Sie die Verbindung wieder her und versuchen Sie es erneut. "
        "Ihre gespeicherten Daten bleiben unverändert."
    ),
}

_DE_DE = {
    **_DE_AT,
    "Research desk": "Recherchebereich",
    "Find a home": "Wohnung oder Haus finden",
    "State or metro area": "Bundesland oder Metropolregion",
}

_ES_CR = {
    "PropertyQuarry Search": "Búsqueda de PropertyQuarry",
    "PropertyQuarry Shortlist": "Favoritos de PropertyQuarry",
    "PropertyQuarry Research": "Investigación de PropertyQuarry",
    "Skip to content": "Saltar al contenido",
    "Product": "Producto",
    "How it works": "Cómo funciona",
    "Pricing": "Precios",
    "Sign in": "Iniciar sesión",
    "Open search": "Abrir búsqueda",
    "Email sign-in": "Iniciar sesión por correo",
    "Your privacy choice": "Su elección de privacidad",
    "Optional analytics help us understand which public pages are useful. They stay off unless you allow them. Essential security and preference cookies still work.": (
        "Los análisis opcionales nos ayudan a entender qué páginas públicas son útiles. "
        "Permanecen desactivados hasta que usted los permita. Las cookies esenciales de "
        "seguridad y preferencias siguen funcionando."
    ),
    "Review cookie settings": "Revisar la configuración de cookies",
    "Reject optional analytics": "Rechazar análisis opcionales",
    "Allow analytics": "Permitir análisis",
    "Search once. See the right homes. Decide faster.": (
        "Busque una vez. Vea las propiedades adecuadas. Decida más rápido."
    ),
    "One brief, matching homes, and the details that matter before a viewing.": (
        "Un perfil, propiedades adecuadas y los detalles importantes antes de una visita."
    ),
    "Brief": "Perfil",
    "Decide": "Decidir",
    "Example shortlist": "Lista de muestra",
    "Tap any row to open the example.": "Seleccione cualquier fila para abrir el ejemplo.",
    "Example": "Ejemplo",
    "Sample homes": "Propiedades de muestra",
    "3 examples": "3 ejemplos",
    "Example home with a 3D tour and walkthrough.": (
        "Propiedad de ejemplo con recorrido 3D y visita guiada."
    ),
    "Quiet layout near transit": "Distribución tranquila cerca del transporte",
    "Good fit. Parking still unclear.": (
        "Buena compatibilidad. El estacionamiento aún no está claro."
    ),
    "Strong price, open questions": "Buen precio, preguntas pendientes",
    "Strong price. Check the details.": "Buen precio. Revise los detalles.",
    "Must-haves stay clear": "Los requisitos indispensables quedan claros",
    "Area, mode, and must-haves can exclude.": (
        "La zona, modalidad y requisitos indispensables pueden excluir resultados."
    ),
    "Preferences shape fit": "Las preferencias definen la compatibilidad",
    "Schools, errands, noise, and commute adjust order.": (
        "Escuelas, diligencias, ruido y desplazamientos ajustan el orden."
    ),
    "Details stay together": "Los detalles permanecen juntos",
    "Costs, floorplans, tours, and open questions stay with the home.": (
        "Costos, planos, recorridos y preguntas pendientes permanecen con la propiedad."
    ),
    "Property onboarding": "Configuración de búsqueda de propiedades",
    "Set up your property search.": "Configure su búsqueda de propiedades.",
    "Create an account, then start with your brief.": (
        "Cree una cuenta y empiece con su perfil de búsqueda."
    ),
    "Email": "Correo electrónico",
    "Create your account.": "Cree su cuenta.",
    "Send verification code": "Enviar código de verificación",
    "Privacy": "Privacidad",
    "Terms": "Términos",
    "Support": "Ayuda",
    "Imprint": "Aviso legal",
    "Cookie settings": "Configuración de cookies",
    "Subprocessors": "Subprocesadores",
    "Refunds": "Reembolsos",
    "Disclaimers": "Descargos de responsabilidad",
    "Ready": "Listo",
    "1. Email": "1. Correo electrónico",
    "2. Details": "2. Detalles",
    "3. Access": "3. Acceso",
    "4. Ready": "4. Listo",
    "Step 1": "Paso 1",
    "Try again": "Intentar de nuevo",
    "Search": "Buscar",
    "Shortlist": "Favoritos",
    "Research": "Investigación",
    "Account": "Cuenta",
    "Research desk": "Mesa de investigación",
    "PropertyQuarry sections": "Secciones de PropertyQuarry",
    "PropertyQuarry public home": "Inicio público de PropertyQuarry",
    "Dark mode": "Modo oscuro",
    "Browser alerts": "Alertas del navegador",
    "Browser alerts on": "Alertas del navegador activadas",
    "Browser alerts off": "Alertas del navegador desactivadas",
    "Light mode": "Modo claro",
    "Stop": "Detener",
    "Searching": "Buscando",
    "Results ready": "Resultados listos",
    "Paused": "En pausa",
    "estimating": "calculando",
    "Waiting for homes.": "Esperando propiedades.",
    "Looking for homes": "Buscando propiedades",
    "Preparing search": "Preparando búsqueda",
    "Preparing search.": "Preparando búsqueda.",
    "Homes": "Propiedades",
    "To review": "Por revisar",
    "Checks left": "Revisiones pendientes",
    "Checked": "Revisado",
    "Sites": "Portales",
    "Updates": "Actualizaciones",
    "Search totals": "Totales de búsqueda",
    "Search progress": "Progreso de búsqueda",
    "Homes found": "Propiedades encontradas",
    "Homes checked": "Propiedades revisadas",
    "Checking the saved search.": "Revisando la búsqueda guardada.",
    "Checking the latest search status.": "Revisando el estado más reciente de la búsqueda.",
    "Saved defaults": "Preferencias guardadas",
    "Access": "Acceso",
    "Billing": "Facturación",
    "Log out": "Cerrar sesión",
    "Account navigation": "Navegación de la cuenta",
    "Launch search": "Iniciar búsqueda",
    "Search brief": "Perfil de búsqueda",
    "Adjust search": "Ajustar búsqueda",
    "Back": "Atrás",
    "Next": "Siguiente",
    "Review": "Revisar",
    "Market": "Mercado",
    "Where": "Dónde",
    "What": "Qué",
    "Strategy": "Estrategia",
    "Guardrails": "Criterios",
    "Details": "Detalles",
    "Reachability": "Accesibilidad",
    "Numbers": "Cifras",
    "Research depth": "Nivel de investigación",
    "Providers": "Portales",
    "Market, listing mode, and budget.": "Mercado, modalidad del anuncio y presupuesto.",
    "Country, market, and target areas.": "País, mercado y zonas objetivo.",
    "Type, budget, size, move-in.": "Tipo, presupuesto, tamaño y fecha de mudanza.",
    "Target areas, spillover distance, and investment guardrails.": "Zonas objetivo, radio adicional y criterios de inversión.",
    "Execution risk, legal context, and which weak listings should be excluded.": "Riesgo de ejecución, contexto legal y anuncios débiles que deben excluirse.",
    "Preferences, family setup, errands, and local-fit rules.": "Preferencias, familia, diligencias y criterios de ajuste local.",
    "How much supporting detail the shortlist needs.": "Cuánto detalle de respaldo necesitan los favoritos.",
    "Destinations, travel modes, and time limits that change fit.": "Destinos, medios de transporte y límites de tiempo que cambian el ajuste.",
    "Return, risk, and external data depth.": "Rendimiento, riesgo y nivel de datos externos.",
    "Risk, supply, and how much detail each strong match should carry.": "Riesgo, oferta y nivel de detalle de cada buen resultado.",
    "Choose providers.": "Elija los portales.",
    "Launching...": "Iniciando búsqueda…",
    "Preparing search...": "Preparando búsqueda…",
    "Starting search...": "Preparando búsqueda…",
    "Launching search...": "Iniciando búsqueda…",
    "Opening results...": "Abriendo resultados…",
    "Opening active search...": "Abriendo la búsqueda activa…",
    "Search started. Opening results...": "Búsqueda iniciada. Abriendo resultados…",
    "Saving...": "Guardando…",
    "Saved.": "Guardado.",
    "Save search": "Guardar búsqueda",
    "Save changes": "Guardar cambios",
    "Save as new": "Guardar como nueva",
    "Reset": "Restablecer",
    "Search flow": "Flujo de búsqueda",
    "Find a home": "Buscar vivienda",
    "Find an investment": "Buscar inversión",
    "What are you looking for?": "¿Qué está buscando?",
    "Country": "País",
    "Austria": "Austria",
    "Germany": "Alemania",
    "Costa Rica": "Costa Rica",
    "Rent": "Alquilar",
    "Buy": "Comprar",
    "Search mode": "Modalidad de búsqueda",
    "Property type": "Tipo de propiedad",
    "Off": "Desactivado",
    "Investment research": "Investigación de inversión",
    "Investment research on buy listings": "Investigación de inversión en propiedades en venta",
    "Investment strategy": "Estrategia de inversión",
    "Best overall opportunity": "Mejor oportunidad general",
    "Cash flow": "Flujo de caja",
    "Appreciation": "Plusvalía",
    "Undervalued": "Subvalorada",
    "Low risk": "Riesgo bajo",
    "State or metro area": "Provincia o área metropolitana",
    "Target areas": "Zonas objetivo",
    "Map": "Mapa",
    "List": "Lista",
    "All areas": "Todas las zonas",
    "Clear": "Limpiar",
    "Done": "Listo",
    "Zoom": "Acercamiento",
    "Meters": "Metros",
    "Kilometers": "Kilómetros",
    "Unit": "Unidad",
    "Add areas manually": "Agregar zonas manualmente",
    "Search sources": "Fuentes de búsqueda",
    "All sites": "Todos los portales",
    "Result mode": "Modo de resultados",
    "Strict shortlist": "Favoritos estrictos",
    "Discovery pass": "Exploración amplia",
    "Load": "Cargar",
    "Save": "Guardar",
    "Saved": "Guardado",
    "What matters": "Lo que importa",
    "Home": "Vivienda",
    "Daily life": "Vida diaria",
    "Checks": "Verificaciones",
    "Neutral": "Neutral",
    "Avoid": "Evitar",
    "Nice to have": "Deseable",
    "Strong wish": "Muy deseable",
    "Must have": "Indispensable",
    "saved homes": "viviendas guardadas",
    "matching homes": "propiedades adecuadas",
    "matching opportunities": "oportunidades adecuadas",
    "Saved pages": "Páginas guardadas",
    "Edit search": "Editar búsqueda",
    "At a glance": "De un vistazo",
    "Next step": "Siguiente paso",
    "Premium next step": "Siguiente paso premium",
    "Best so far": "La mejor hasta ahora",
    "Diorama preview in progress": "Creando vista previa del diorama",
    "Fit": "Compatibilidad",
    "Monthly total": "Total mensual",
    "It is meaningfully cheaper.": "Es considerablemente más económica.",
    "Tour not available yet.": "El recorrido aún no está disponible.",
    "Open listing": "Abrir anuncio",
    "Open the property page for more context.": "Abra la página de la propiedad para ver más detalles.",
    "No price published": "Precio no publicado",
    "3D tour available": "Recorrido 3D disponible",
    "3D tour queued": "Recorrido 3D en cola",
    "Request 3D tour": "Solicitar recorrido 3D",
    "Preparing 3D tour.": "Preparando recorrido 3D.",
    "Walkthrough available": "Recorrido guiado disponible",
    "Walkthrough queued": "Recorrido guiado en cola",
    "Request walkthrough": "Solicitar recorrido guiado",
    "Request when you want a guided walkthrough.": "Solicítelo cuando desee un recorrido guiado.",
    "More": "Más",
    "Selected property": "Propiedad seleccionada",
    "Provider": "Proveedor",
    "Operating costs not listed": "Gastos operativos no indicados",
    "Media preview not available.": "La vista previa multimedia aún no está disponible.",
    "The listing is still available, and your saved property details are unchanged.": "El anuncio sigue disponible y los detalles guardados de la propiedad no cambiaron.",
    "Best part": "Punto fuerte",
    "Keep in mind": "Tenga en cuenta",
    "Price level": "Nivel de precio",
    "Return": "Rendimiento",
    "Not available yet.": "Aún no disponible.",
    "This home matches the current brief.": "Esta propiedad coincide con el perfil de búsqueda actual.",
    "This opportunity matches the current brief.": "Esta oportunidad coincide con el perfil de búsqueda actual.",
    "No major drawback yet.": "Aún no se detectó una desventaja importante.",
    "Next question": "Siguiente pregunta",
    "Next to check": "Siguiente verificación",
    "Financial picture": "Panorama financiero",
    "Market data": "Datos del mercado",
    "Tune search": "Ajustar búsqueda",
    "Hide search settings": "Ocultar configuración de búsqueda",
    "Viewing requested": "Visita solicitada",
    "Documents requested": "Documentos solicitados",
    "Offer candidate": "Candidata para oferta",
    "Archived": "Archivada",
    "Property decision reaction": "Reacción sobre la propiedad",
    "Hide": "Ocultar",
    "Choose one so the next search gets better.": "Elija una opción para mejorar la próxima búsqueda.",
    "What worked": "Lo que funcionó",
    "Loading the next things to check.": "Cargando los siguientes puntos por verificar.",
    "What should change next time?": "¿Qué debería cambiar la próxima vez?",
    "Save decision": "Guardar decisión",
    "Only pay for the next move when the finished search or selected home needs it.": "Pague solo por el siguiente paso cuando la búsqueda terminada o la propiedad seleccionada lo necesiten.",
    "Upgrade": "Mejorar plan",
    "Premium market report": "Informe de mercado premium",
    "Offer": "Oferta",
    "Open checkout": "Ir al pago",
    "Concierge shortlist refresh": "Actualización concierge de preselección",
    "EUR 99 | Use a paid rerun only when this shortlist needs a sharper second pass.": "EUR 99 | Use una repetición de pago solo si esta preselección necesita una segunda revisión más precisa.",
    "Agent-ready export": "Exportación lista para agente",
    "EUR 19 | Export the selected result only when a tighter handoff would save real time.": "EUR 19 | Exporte el resultado seleccionado solo si una entrega más concisa realmente ahorra tiempo.",
    "Share this home": "Compartir esta propiedad",
    "Share results": "Compartir resultados",
    "Best points": "Puntos destacados",
    "No match notes yet.": "Aún no hay notas de compatibilidad.",
    "Facts": "Datos",
    "Signed in": "Sesión iniciada",
    "Current property": "Propiedad actual",
    "Living area": "Área habitable",
    "Rooms": "Habitaciones",
    "Costs": "Costos",
    "Features": "Características",
    "Why it works": "Por qué funciona",
    "Match": "Coincidencia",
    "Nothing else yet.": "Aún no hay más observaciones.",
    "Around it": "Alrededores",
    "About the home": "Sobre la propiedad",
    "Energy": "Energía",
    "Timeline": "Cronología",
    "No tracked question yet": "Aún no hay preguntas en seguimiento",
    "Use Clippy or Ask agent next to start a tracked follow-up.": "Use Clippy o «Preguntar al agente» para iniciar un seguimiento.",
    "Found on list": "Encontrada en la lista",
    "360 state": "Estado 360°",
    "360 gap": "Falta de 360°",
    "Unavailable | Tour not available yet.": "No disponible | El recorrido aún no está disponible.",
    "Property page ready": "Página de la propiedad lista",
    "The property page is ready for household or advisor follow-up.": "La página de la propiedad está lista para revisarla en familia o con asesoría.",
    "Things to watch": "Aspectos por vigilar",
    "Visuals": "Visualizaciones",
    "Request 3D tour or walkthrough": "Solicitar recorrido 3D o guiado",
    "Visuals not published yet": "Visualizaciones aún no publicadas",
    "Visuals not available yet": "Visualizaciones aún no disponibles",
    "Area": "Zona",
    "Area map": "Mapa de la zona",
    "Map and nearby context.": "Mapa y contexto cercano.",
    "Switch layers.": "Cambiar capas.",
    "Showing available layers.": "Mostrando las capas disponibles.",
    "Floorplan": "Plano",
    "medium": "medio",
    "Unavailable": "No disponible",
    "Open score guide": "Abrir guía de puntaje",
    "Start search": "Iniciar búsqueda",
    "Property research": "Investigación de la propiedad",
    "Research updated": "Investigación actualizada",
    "Research packet": "Paquete de investigación",
    "Back to shortlist": "Volver a favoritos",
    "Open property": "Abrir propiedad",
    "Evidence": "Evidencia",
    "Decision": "Decisión",
    "Reactions": "Reacciones",
    "What changed": "Qué cambió",
    "Follow-up": "Seguimiento",
    "Yes": "Sí",
    "No": "No",
    "Maybe": "Tal vez",
    "Request documents": "Solicitar documentos",
    "View shortlist": "Ver favoritos",
    "You’re offline.": "Está sin conexión.",
    "Keep this page open. Reconnect, then try again. Your saved work is unchanged.": (
        "Mantenga esta página abierta. Vuelva a conectarse e inténtelo de nuevo. "
        "Su trabajo guardado no cambia."
    ),
}

_TRANSLATIONS = {
    "de-AT": _DE_AT,
    "de-DE": _DE_DE,
    "es-CR": _ES_CR,
}

_LOCALIZED_SEO_COPY = {
    "/": {
        "en": (
            "PropertyQuarry — Find, compare, and decide on property",
            "Define your search, compare the strongest homes, open evidence-rich research, and keep every property decision in one workspace.",
        ),
        "de-AT": (
            "PropertyQuarry – Immobilien finden, vergleichen und entscheiden",
            "Legen Sie Ihre Suche fest, vergleichen Sie passende Immobilien und treffen Sie Entscheidungen mit nachvollziehbaren Unterlagen an einem Ort.",
        ),
        "de-DE": (
            "PropertyQuarry – Immobilien finden, vergleichen und entscheiden",
            "Legen Sie Ihre Suche fest, vergleichen Sie passende Immobilien und treffen Sie Entscheidungen mit nachvollziehbaren Unterlagen an einem Ort.",
        ),
        "es-CR": (
            "PropertyQuarry — Encuentre, compare y decida sobre propiedades",
            "Defina su búsqueda, compare las propiedades más adecuadas y tome decisiones con investigación verificable en un solo espacio.",
        ),
    },
    "/pricing": {
        "en": (
            "PropertyQuarry Pricing",
            "Compare PropertyQuarry plans by search coverage, research depth, property pages, and optional advanced visual features.",
        ),
        "de-AT": (
            "PropertyQuarry Preise",
            "Vergleichen Sie PropertyQuarry Tarife nach Suchabdeckung, Recherchetiefe, Immobilienseiten und optionalen erweiterten Visualisierungen.",
        ),
        "de-DE": (
            "PropertyQuarry Preise",
            "Vergleichen Sie PropertyQuarry Tarife nach Suchabdeckung, Recherchetiefe, Immobilienseiten und optionalen erweiterten Visualisierungen.",
        ),
        "es-CR": (
            "Precios de PropertyQuarry",
            "Compare los planes de PropertyQuarry por cobertura de búsqueda, profundidad de investigación, páginas de propiedades y funciones visuales opcionales.",
        ),
    },
    "/security": {
        "en": (
            "PropertyQuarry Security",
            "Review how PropertyQuarry protects account access, private searches, controlled sharing, and customer data.",
        ),
        "de-AT": (
            "PropertyQuarry Sicherheit",
            "Erfahren Sie, wie PropertyQuarry Kontozugriffe, private Suchen, kontrollierte Freigaben und Kundendaten schützt.",
        ),
        "de-DE": (
            "PropertyQuarry Sicherheit",
            "Erfahren Sie, wie PropertyQuarry Kontozugriffe, private Suchen, kontrollierte Freigaben und Kundendaten schützt.",
        ),
        "es-CR": (
            "Seguridad de PropertyQuarry",
            "Conozca cómo PropertyQuarry protege el acceso a la cuenta, las búsquedas privadas, el uso compartido y los datos de clientes.",
        ),
    },
    "/support": {
        "en": (
            "PropertyQuarry Support",
            "Get help with account access, searches, research, billing handoffs, and property workflows.",
        ),
        "de-AT": (
            "PropertyQuarry Support",
            "Erhalten Sie Hilfe bei Kontozugriff, Suche, Recherche, Abrechnung und Immobilienabläufen.",
        ),
        "de-DE": (
            "PropertyQuarry Support",
            "Erhalten Sie Hilfe bei Kontozugriff, Suche, Recherche, Abrechnung und Immobilienabläufen.",
        ),
        "es-CR": (
            "Soporte de PropertyQuarry",
            "Obtenga ayuda con el acceso a la cuenta, las búsquedas, la investigación, la facturación y los flujos de propiedades.",
        ),
    },
    "/integrations": {
        "en": (
            "PropertyQuarry Integrations",
            "See which PropertyQuarry integrations are available, which remain guided, and where review is required.",
        ),
        "de-AT": (
            "PropertyQuarry Integrationen",
            "Sehen Sie, welche PropertyQuarry Integrationen verfügbar oder begleitet sind und wo eine Prüfung erforderlich bleibt.",
        ),
        "de-DE": (
            "PropertyQuarry Integrationen",
            "Sehen Sie, welche PropertyQuarry Integrationen verfügbar oder begleitet sind und wo eine Prüfung erforderlich bleibt.",
        ),
        "es-CR": (
            "Integraciones de PropertyQuarry",
            "Conozca qué integraciones están disponibles o guiadas y dónde se requiere una revisión.",
        ),
    },
    "/docs": {
        "en": (
            "PropertyQuarry Docs",
            "Read the public guide to PropertyQuarry features, workflows, product boundaries, and evidence handling.",
        ),
        "de-AT": (
            "PropertyQuarry Dokumentation",
            "Lesen Sie den öffentlichen Leitfaden zu Funktionen, Abläufen, Produktgrenzen und Nachweisen in PropertyQuarry.",
        ),
        "de-DE": (
            "PropertyQuarry Dokumentation",
            "Lesen Sie den öffentlichen Leitfaden zu Funktionen, Abläufen, Produktgrenzen und Nachweisen in PropertyQuarry.",
        ),
        "es-CR": (
            "Documentación de PropertyQuarry",
            "Consulte la guía pública sobre funciones, flujos, límites del producto y manejo de evidencia en PropertyQuarry.",
        ),
    },
    "/guides/wohnung-kaufen-wien-checkliste": {
        "en": (
            "Vienna apartment purchase checklist | PropertyQuarry",
            "Use a structured checklist for documents, condition, location, financing, and follow-up when buying an apartment in Vienna.",
        ),
        "de-AT": (
            "Checkliste für den Wohnungskauf in Wien | PropertyQuarry",
            "Prüfen Sie Unterlagen, Zustand, Lage, Finanzierung und nächste Schritte beim Wohnungskauf in Wien mit einer klaren Checkliste.",
        ),
        "de-DE": (
            "Checkliste für den Wohnungskauf in Wien | PropertyQuarry",
            "Prüfen Sie Unterlagen, Zustand, Lage, Finanzierung und nächste Schritte beim Wohnungskauf in Wien mit einer klaren Checkliste.",
        ),
        "es-CR": (
            "Lista para comprar un apartamento en Viena | PropertyQuarry",
            "Revise documentos, condición, ubicación, financiamiento y próximos pasos al comprar un apartamento en Viena.",
        ),
    },
    "/markets/vienna": {
        "en": (
            "Vienna property search | PropertyQuarry",
            "Explore a structured Vienna property search with district context, comparable homes, evidence, and saved decisions.",
        ),
        "de-AT": (
            "Immobiliensuche in Wien | PropertyQuarry",
            "Suchen Sie strukturiert nach Immobilien in Wien – mit Bezirkskontext, vergleichbaren Objekten, Nachweisen und gespeicherten Entscheidungen.",
        ),
        "de-DE": (
            "Immobiliensuche in Wien | PropertyQuarry",
            "Suchen Sie strukturiert nach Immobilien in Wien – mit Bezirkskontext, vergleichbaren Objekten, Nachweisen und gespeicherten Entscheidungen.",
        ),
        "es-CR": (
            "Búsqueda de propiedades en Viena | PropertyQuarry",
            "Explore propiedades en Viena con contexto por distrito, comparables, evidencia y decisiones guardadas.",
        ),
    },
}

_STATUS_COPY = {
    "en": (
        "Language and translation status",
        "This route uses the English source interface. Legal terms and provider-specific copy remain in English.",
        "Localized interface copy has not been professionally reviewed.",
    ),
    "de-AT": (
        "Sprache und Übersetzungsstatus",
        "Die zentrale Benutzeroberfläche ist auf Deutsch verfügbar. Rechtliche Hinweise, Anbietertexte und noch nicht übersetzte Inhalte bleiben auf Englisch.",
        "Die Übersetzung wurde nicht professionell geprüft.",
    ),
    "de-DE": (
        "Sprache und Übersetzungsstatus",
        "Die zentrale Benutzeroberfläche ist auf Deutsch verfügbar. Rechtliche Hinweise, Anbietertexte und noch nicht übersetzte Inhalte bleiben auf Englisch.",
        "Die Übersetzung wurde nicht professionell geprüft.",
    ),
    "es-CR": (
        "Idioma y estado de traducción",
        "La interfaz principal está disponible en español. Los textos legales, de proveedores y aún no traducidos permanecen en inglés.",
        "La traducción no ha sido revisada profesionalmente.",
    ),
}

_PROTECTED_BLOCK_RE = re.compile(
    r"(<script\b[^>]*>.*?</script\s*>"
    r"|<style\b[^>]*>.*?</style\s*>"
    r"|<pre\b[^>]*>.*?</pre\s*>"
    r"|<code\b[^>]*>.*?</code\s*>"
    r"|<textarea\b[^>]*>.*?</textarea\s*>"
    r"|<template\b[^>]*>.*?</template\s*>"
    r"|<svg\b[^>]*>.*?</svg\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_TEXT_NODE_RE = re.compile(r">(?P<text>[^<>]+)<")
_TRANSLATABLE_ATTRIBUTE_RE = re.compile(
    r"(?P<prefix>\b(?P<name>href|action|aria-label|title|placeholder)\s*=\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<html\b(?P<attrs>[^>]*)>", re.IGNORECASE)
_HTML_LANG_RE = re.compile(r"\s+lang\s*=\s*(['\"]).*?\1", re.IGNORECASE | re.DOTALL)
_HEAD_CONTENT_RE = re.compile(r"<head\b[^>]*>(?P<content>.*?)</head\s*>", re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE | re.DOTALL)
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE | re.DOTALL)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>.*?</title\s*>", re.IGNORECASE | re.DOTALL)
_TAG_ATTRIBUTE_RE = re.compile(
    r"\b(?P<name>[A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_TRANSLATED_ROUTE_RE = re.compile(
    r"^(?:"
    r"/|"
    r"/(?:pricing|security|support|integrations|docs|sign-in|register)|"
    r"/guides/wohnung-kaufen-wien-checkliste|"
    r"/markets/vienna|"
    r"/app/(?:search|properties|shortlist|agents|alerts|research|account|billing|support)|"
    r"/app/settings/(?:google|access|usage|support|trust|invitations|outcomes|plan)|"
    r"/app/properties/(?:packets|notifications/preview)|"
    r"/app/research/[^/]+|"
    r"/app/shortlist/run/[^/]+|"
    r"/tours/[^/]+"
    r")/*$"
)
_REQUIRED_ROUTE_RE = re.compile(
    r"^(?:"
    r"/|"
    r"/(?:pricing|security|support|privacy|terms|cookies|subprocessors|refunds|disclaimers|imprint|integrations|docs|sign-in|register)|"
    r"/guides/wohnung-kaufen-wien-checkliste|"
    r"/markets/vienna|"
    r"/app/(?:search|properties|shortlist|agents|alerts|research|account|billing|support)|"
    r"/app/settings/(?:google|access|usage|support|trust|invitations|outcomes|plan)|"
    r"/app/properties/(?:packets|notifications/preview)|"
    r"/app/research/[^/]+|"
    r"/app/shortlist/run/[^/]+|"
    r"/tours/[^/]+"
    r")/*$"
)
_LEGAL_ENGLISH_SOURCE_ROUTE_RE = re.compile(
    r"^/(?:privacy|terms|cookies|subprocessors|refunds|disclaimers|imprint)/*$"
)
_INDEXABLE_LOCALIZED_ROUTES = frozenset(
    {
        "/",
        "/pricing",
        "/security",
        "/support",
        "/integrations",
        "/docs",
        "/guides/wohnung-kaufen-wien-checkliste",
        "/markets/vienna",
    }
)
_LOCALIZATION_SLOT = '<span data-pq-localization-slot></span>'
_SAFE_QUERY_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_LOCALE_SELECTOR_QUERY_KEYS = frozenset(
    {"run_id", "home", "pane", "autoplay", "view", "sort", "page"}
)
_SENSITIVE_QUERY_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "auth",
    "email",
    "code",
    "key",
    "error",
    "return",
)
_ACCEPT_LANGUAGE_Q_RE = re.compile(r"^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$")
_PSEUDO_ACCENTS = str.maketrans(
    {
        "a": "à",
        "b": "ƀ",
        "c": "ç",
        "d": "ď",
        "e": "ë",
        "f": "ƒ",
        "g": "ğ",
        "h": "ħ",
        "i": "ï",
        "j": "ĵ",
        "k": "ķ",
        "l": "ľ",
        "m": "ɱ",
        "n": "ñ",
        "o": "ô",
        "p": "þ",
        "q": "ɋ",
        "r": "ř",
        "s": "š",
        "t": "ŧ",
        "u": "ü",
        "v": "ṽ",
        "w": "ŵ",
        "x": "ẋ",
        "y": "ÿ",
        "z": "ž",
        "A": "À",
        "E": "Ë",
        "I": "Ï",
        "O": "Ô",
        "U": "Ü",
    }
)


@dataclass(frozen=True)
class PropertyQuarryLocaleDecision:
    locale: str
    source: str
    query_locale_valid: bool = False
    query_locale_rejected: bool = False


def normalize_propertyquarry_locale(value: object, *, allow_pseudo: bool = False) -> str | None:
    raw = str(value or "").strip().replace("_", "-")
    if not raw or len(raw) > 32 or any(ord(character) < 32 for character in raw):
        return None
    if allow_pseudo and raw.casefold() == PROPERTYQUARRY_PSEUDO_LOCALE:
        return PROPERTYQUARRY_PSEUDO_LOCALE
    return _LOCALE_ALIASES.get(raw.casefold())


def _parse_query_pairs(query_string: bytes | str) -> list[tuple[str, str]]:
    raw = query_string.decode("utf-8", errors="replace") if isinstance(query_string, bytes) else str(query_string or "")
    try:
        return urllib.parse.parse_qsl(raw[:8192], keep_blank_values=True, max_num_fields=64)
    except ValueError:
        return []


def _header_value(headers: list[tuple[bytes, bytes]], name: str) -> str:
    expected = name.lower().encode("ascii")
    values = [value.decode("latin-1") for key, value in headers if key.lower() == expected]
    return ",".join(values)


def _cookie_locale(raw_cookie: str) -> str | None:
    if not raw_cookie or len(raw_cookie) > 8192:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except CookieError:
        return None
    morsel = cookie.get(PROPERTYQUARRY_LOCALE_COOKIE)
    return normalize_propertyquarry_locale(morsel.value if morsel is not None else "")


def _accept_language_locale(raw_header: str) -> str | None:
    ranked: list[tuple[float, int, str]] = []
    for position, part in enumerate(str(raw_header or "")[:1024].split(",")[:16]):
        pieces = [piece.strip() for piece in part.split(";") if piece.strip()]
        if not pieces or pieces[0] == "*":
            continue
        quality = 1.0
        quality_seen = False
        quality_valid = True
        for parameter in pieces[1:]:
            name, separator, raw_quality = parameter.partition("=")
            if name.strip().casefold() != "q":
                continue
            if (
                quality_seen
                or not separator
                or not _ACCEPT_LANGUAGE_Q_RE.fullmatch(raw_quality.strip())
            ):
                quality_valid = False
                break
            quality_seen = True
            quality = float(raw_quality.strip())
        if not quality_valid:
            continue
        locale = normalize_propertyquarry_locale(pieces[0])
        if locale is not None and quality > 0:
            ranked.append((quality, -position, locale))
    return max(ranked)[2] if ranked else None


def resolve_propertyquarry_locale(
    *,
    query_string: bytes | str = b"",
    headers: list[tuple[bytes, bytes]] | tuple[tuple[bytes, bytes], ...] = (),
) -> PropertyQuarryLocaleDecision:
    header_list = list(headers)
    query_values = [value for key, value in _parse_query_pairs(query_string) if key == "lang"]
    if query_values:
        query_locale = normalize_propertyquarry_locale(query_values[-1])
        if query_locale is not None:
            return PropertyQuarryLocaleDecision(query_locale, "query", query_locale_valid=True)
    cookie_locale = _cookie_locale(_header_value(header_list, "cookie"))
    if cookie_locale is not None:
        return PropertyQuarryLocaleDecision(
            cookie_locale,
            "cookie",
            query_locale_rejected=bool(query_values),
        )
    accept_language_locale = _accept_language_locale(
        _header_value(header_list, "accept-language")
    )
    if accept_language_locale is not None:
        return PropertyQuarryLocaleDecision(
            accept_language_locale,
            "accept_language",
            query_locale_rejected=bool(query_values),
        )
    return PropertyQuarryLocaleDecision("en", "default", query_locale_rejected=bool(query_values))


def propertyquarry_locale_cookie_header(locale: str, *, secure: bool) -> str:
    normalized = normalize_propertyquarry_locale(locale)
    if normalized is None:
        raise ValueError("unsupported_propertyquarry_locale")
    parts = [
        f"{PROPERTYQUARRY_LOCALE_COOKIE}={normalized}",
        "Path=/",
        "Max-Age=15552000",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def propertyquarry_route_is_translated(path: str) -> bool:
    return bool(_TRANSLATED_ROUTE_RE.fullmatch(str(path or "")))


def propertyquarry_required_route_translation_status(path: str) -> str:
    normalized = str(path or "")
    if not _REQUIRED_ROUTE_RE.fullmatch(normalized):
        return "not_in_global_experience_contract"
    if _LEGAL_ENGLISH_SOURCE_ROUTE_RE.fullmatch(normalized):
        return "blocked_unreviewed_legal_source"
    if _TRANSLATED_ROUTE_RE.fullmatch(normalized):
        return "localized_route_shell_pending_native_review"
    return "blocked_missing_route_localization"


def _pseudo_localize(value: str) -> str:
    accented = value.translate(_PSEUDO_ACCENTS)
    expanded = re.sub(r"([àëïôüÀËÏÔÜ])", r"\1\1", accented)
    return f"[!! {expanded} — ẋẋ !!]"


def propertyquarry_translation(value: str, *, locale: str) -> str:
    normalized = normalize_propertyquarry_locale(locale, allow_pseudo=True)
    if normalized is None:
        raise ValueError("unsupported_propertyquarry_locale")
    if normalized == PROPERTYQUARRY_PSEUDO_LOCALE:
        return _pseudo_localize(str(value))
    if normalized == "en":
        return str(value)
    source = str(value)
    translated = _TRANSLATIONS[normalized].get(source)
    if translated is not None:
        return translated
    is_german = normalized.startswith("de-")
    matching_total = re.fullmatch(
        r"(\d+)\s+(matching homes|matching opportunities|saved homes|saved opportunities)",
        source,
        flags=re.IGNORECASE,
    )
    if matching_total:
        unit = matching_total.group(2).casefold()
        if is_german:
            label = {
                "matching homes": "passende Immobilien",
                "matching opportunities": "passende Anlagechancen",
                "saved homes": "gespeicherte Immobilien",
                "saved opportunities": "gespeicherte Anlagechancen",
            }[unit]
        else:
            label = {
                "matching homes": "propiedades adecuadas",
                "matching opportunities": "oportunidades adecuadas",
                "saved homes": "propiedades guardadas",
                "saved opportunities": "oportunidades guardadas",
            }[unit]
        return f"{matching_total.group(1)} {label}"
    source_progress = re.fullmatch(
        r"(\d+)\s*/\s*(\d+)\s+(sources|search queries)",
        source,
        flags=re.IGNORECASE,
    )
    if source_progress:
        unit = source_progress.group(3).casefold()
        if is_german:
            label = "Quellen" if unit == "sources" else "Suchanfragen"
        else:
            label = "fuentes" if unit == "sources" else "consultas de búsqueda"
        return f"{source_progress.group(1)} / {source_progress.group(2)} {label}"
    rooms = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s+rooms?(\s*\|\s*.+)?", source, flags=re.IGNORECASE)
    if rooms:
        unit = "Zimmer" if is_german else ("habitación" if rooms.group(1) == "1" else "habitaciones")
        return f"{rooms.group(1)} {unit}{rooms.group(2) or ''}"
    fit_score = re.fullmatch(r"Fit\s+(\d+)", source, flags=re.IGNORECASE)
    if fit_score:
        return f"{'Eignung' if is_german else 'Compatibilidad'} {fit_score.group(1)}"
    monthly_total = re.fullmatch(r"Monthly total\s+(.+)", source, flags=re.IGNORECASE)
    if monthly_total:
        return f"{'Monatlich gesamt' if is_german else 'Total mensual'} {monthly_total.group(1)}"
    match_total = re.fullmatch(r"(\d+)\s+matches?", source, flags=re.IGNORECASE)
    if match_total:
        return (
            f"{match_total.group(1)} Treffer"
            if is_german
            else f"{match_total.group(1)} coincidencias"
        )
    review_title = re.fullmatch(r"Review\s+(.+)", source, flags=re.IGNORECASE)
    if review_title:
        return f"{'Prüfen' if is_german else 'Revisar'}: {review_title.group(1)}"
    area_map_title = re.fullmatch(r"Open area map for\s+(.+)", source, flags=re.IGNORECASE)
    if area_map_title:
        return (
            f"Gebietskarte öffnen: {area_map_title.group(1)}"
            if is_german
            else f"Abrir mapa de la zona: {area_map_title.group(1)}"
        )
    premium_market_context = re.fullmatch(
        r"(EUR\s+\d+(?:[.,]\d+)?\s*\|\s*)Use only if\s+(.+)\s+needs wider market context before deciding\.",
        source,
        flags=re.IGNORECASE,
    )
    if premium_market_context:
        prefix, title = premium_market_context.groups()
        return (
            f"{prefix}Nur verwenden, wenn {title} vor der Entscheidung einen breiteren Marktkontext benötigt."
            if is_german
            else f"{prefix}Úselo solo si {title} necesita un contexto de mercado más amplio antes de decidir."
        )
    missing_fields = re.fullmatch(r"Missing\s+(.+)", source, flags=re.IGNORECASE)
    if missing_fields:
        replacements = (
            {
                "360 pending": "360°-Ansicht ausstehend",
                "address": "Adresse",
                "heating": "Heizung",
            }
            if is_german
            else {
                "360 pending": "vista 360 pendiente",
                "address": "dirección",
                "heating": "calefacción",
            }
        )
        fields = ", ".join(
            replacements.get(part.strip().casefold(), part.strip())
            for part in missing_fields.group(1).split(",")
        )
        return f"{'Fehlend' if is_german else 'Falta'}: {fields}"
    return source


def propertyquarry_translation_coverage(locale: str) -> dict[str, object]:
    normalized = normalize_propertyquarry_locale(locale, allow_pseudo=True)
    if normalized is None:
        raise ValueError("unsupported_propertyquarry_locale")
    source_messages = frozenset(_DE_AT)
    translated_messages = source_messages if normalized in {"en", PROPERTYQUARRY_PSEUDO_LOCALE} else frozenset(_TRANSLATIONS[normalized])
    route_statuses = {
        route: propertyquarry_required_route_translation_status(
            route.replace("{candidate_ref}", "candidate-ref")
            .replace("{run_id}", "run-id")
            .replace("{slug}", "tour-slug")
        )
        for route in PROPERTYQUARRY_REQUIRED_CUSTOMER_ROUTE_TEMPLATES
    }
    localized_route_count = sum(
        status == "localized_route_shell_pending_native_review"
        for status in route_statuses.values()
    )
    blocked_routes = sorted(
        route
        for route, status in route_statuses.items()
        if status != "localized_route_shell_pending_native_review"
    )
    return {
        "locale": normalized,
        "critical_source_messages": len(source_messages),
        "critical_translated_messages": len(source_messages.intersection(translated_messages)),
        "missing_critical_messages": sorted(source_messages.difference(translated_messages)),
        "coverage_scope": "global_required_route_shell",
        "required_customer_route_count": len(PROPERTYQUARRY_REQUIRED_CUSTOMER_ROUTE_TEMPLATES),
        "localized_route_shell_count": localized_route_count,
        "blocked_required_routes": blocked_routes,
        "route_statuses": route_statuses,
        "localized_indexable_route_count": len(_INDEXABLE_LOCALIZED_ROUTES),
        "english_fallback_scopes": [
            "unreviewed_legal_source",
            "provider_specific",
            "customer_or_listing_content",
        ],
        "professional_review": False,
        "native_launch_ready": False,
    }


def _safe_query_pairs(query_string: bytes | str) -> list[tuple[str, str]]:
    safe: list[tuple[str, str]] = []
    for key, value in _parse_query_pairs(query_string):
        lowered_key = key.casefold()
        if (
            key == "lang"
            or lowered_key not in _SAFE_LOCALE_SELECTOR_QUERY_KEYS
            or not _SAFE_QUERY_KEY_RE.fullmatch(key)
        ):
            continue
        if any(part in lowered_key for part in _SENSITIVE_QUERY_KEY_PARTS):
            continue
        if len(value) > 512 or any(ord(character) < 32 for character in value):
            continue
        safe.append((key, value))
    return safe[:32]


def _route_url(
    *,
    path: str,
    query_string: bytes | str,
    locale: str,
) -> str:
    query = _safe_query_pairs(query_string)
    query.append(("lang", locale))
    return urllib.parse.urlunsplit(("", "", path, urllib.parse.urlencode(query), ""))


def _localized_internal_url(value: str, *, locale: str) -> str:
    raw = html.unescape(str(value or "").strip())
    if not raw or raw.startswith(("#", "//")):
        return raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        return raw
    path = parsed.path
    if path == "/app":
        route_is_localized = True
    else:
        route_is_localized = propertyquarry_route_is_translated(path)
    if not route_is_localized or path.startswith(
        ("/app/api", "/app/assets", "/app/actions", "/tours/files")
    ):
        return raw
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(
        any(part in key.casefold() for part in _SENSITIVE_QUERY_KEY_PARTS)
        for key, _item in query
    ):
        return raw
    query = [(key, item) for key, item in query if key != "lang"]
    query.append(("lang", locale))
    return urllib.parse.urlunsplit(("", "", path, urllib.parse.urlencode(query), parsed.fragment))


def _translate_text_node(raw_text: str, *, locale: str) -> str:
    if not raw_text.strip():
        return raw_text
    leading = raw_text[: len(raw_text) - len(raw_text.lstrip())]
    trailing = raw_text[len(raw_text.rstrip()) :]
    normalized_text = " ".join(html.unescape(raw_text.strip()).split())
    translated = propertyquarry_translation(normalized_text, locale=locale)
    if translated == normalized_text and locale != PROPERTYQUARRY_PSEUDO_LOCALE:
        return raw_text
    return f"{leading}{html.escape(translated, quote=False)}{trailing}"


def _localize_unprotected_html(segment: str, *, locale: str, preserve_locale_in_urls: bool) -> str:
    def replace_attribute(match: re.Match[str]) -> str:
        name = match.group("name").lower()
        value = match.group("value")
        if name in {"href", "action"}:
            if not preserve_locale_in_urls:
                return match.group(0)
            translated_value = _localized_internal_url(value, locale=locale)
        else:
            normalized_value = " ".join(html.unescape(value).split())
            translated_value = propertyquarry_translation(normalized_value, locale=locale)
            if translated_value == normalized_value and locale != PROPERTYQUARRY_PSEUDO_LOCALE:
                return match.group(0)
        return f"{match.group('prefix')}{match.group('quote')}{html.escape(translated_value, quote=True)}{match.group('quote')}"

    with_attributes = _TRANSLATABLE_ATTRIBUTE_RE.sub(replace_attribute, segment)
    return _TEXT_NODE_RE.sub(
        lambda match: f">{_translate_text_node(match.group('text'), locale=locale)}<",
        with_attributes,
    )


def _set_html_lang(document: str, locale: str) -> str:
    def replace(match: re.Match[str]) -> str:
        attrs = _HTML_LANG_RE.sub("", match.group("attrs"))
        return f'<html{attrs} lang="{html.escape(locale, quote=True)}">'

    return _HTML_TAG_RE.sub(replace, document, count=1)


def _replace_meta_content(
    head_content: str,
    *,
    attribute_name: str,
    attribute_value: str,
    content: str,
) -> tuple[str, bool]:
    expected_name = attribute_name.casefold()
    expected_value = attribute_value.casefold()
    replaced = False

    def replace_tag(match: re.Match[str]) -> str:
        nonlocal replaced
        tag = match.group(0)
        attributes = {
            item.group("name").casefold(): html.unescape(item.group("value")).strip()
            for item in _TAG_ATTRIBUTE_RE.finditer(tag)
        }
        if attributes.get(expected_name, "").casefold() != expected_value:
            return tag
        content_attribute = next(
            (
                item
                for item in _TAG_ATTRIBUTE_RE.finditer(tag)
                if item.group("name").casefold() == "content"
            ),
            None,
        )
        if content_attribute is None:
            return tag
        escaped = html.escape(content, quote=True)
        replaced = True
        return (
            tag[: content_attribute.start("value")]
            + escaped
            + tag[content_attribute.end("value") :]
        )

    return _META_TAG_RE.sub(replace_tag, head_content), replaced


def _localized_seo_url(path: str, locale: str) -> str:
    base = f"{PROPERTYQUARRY_PUBLIC_ORIGIN}{path}"
    if locale == "en":
        return base
    return f"{base}?{urllib.parse.urlencode({'lang': locale})}"


def _localized_seo_head_markup(*, path: str, locale: str) -> str:
    canonical = _localized_seo_url(path, locale)
    alternates = [
        (
            target_locale,
            _localized_seo_url(path, target_locale),
        )
        for target_locale in PROPERTYQUARRY_PUBLIC_LOCALES
    ]
    alternates.append(("x-default", _localized_seo_url(path, "en")))
    links = "".join(
        '<link data-pq-localization-seo rel="alternate" '
        f'hreflang="{html.escape(hreflang, quote=True)}" '
        f'href="{html.escape(href, quote=True)}">'
        for hreflang, href in alternates
    )
    og_locale = {
        "en": "en_US",
        "de-AT": "de_AT",
        "de-DE": "de_DE",
        "es-CR": "es_CR",
    }[locale]
    return (
        '<link data-pq-localization-seo rel="canonical" '
        f'href="{html.escape(canonical, quote=True)}">'
        f"{links}"
        '<meta data-pq-localization-seo property="og:locale" '
        f'content="{html.escape(og_locale, quote=True)}">'
    )


def _apply_localized_seo(document: str, *, path: str, locale: str) -> str:
    route_copy = _LOCALIZED_SEO_COPY.get(path, {}).get(locale)
    if path not in _INDEXABLE_LOCALIZED_ROUTES or route_copy is None:
        return document
    head_match = _HEAD_CONTENT_RE.search(document)
    if head_match is None:
        return document
    title, description = route_copy
    head_content = head_match.group("content")
    escaped_title = html.escape(title, quote=False)
    if _TITLE_TAG_RE.search(head_content):
        head_content = _TITLE_TAG_RE.sub(
            f"<title>{escaped_title}</title>", head_content, count=1
        )
    else:
        head_content = f"<title>{escaped_title}</title>{head_content}"
    for attribute_name, attribute_value, value in (
        ("name", "description", description),
        ("property", "og:title", title),
        ("property", "og:description", description),
        ("property", "og:url", _localized_seo_url(path, locale)),
        ("name", "twitter:title", title),
        ("name", "twitter:description", description),
    ):
        head_content, found = _replace_meta_content(
            head_content,
            attribute_name=attribute_name,
            attribute_value=attribute_value,
            content=value,
        )
        if not found:
            head_content += (
                f'<meta data-pq-localization-seo {attribute_name}="'
                f'{html.escape(attribute_value, quote=True)}" content="'
                f'{html.escape(value, quote=True)}">'
            )

    def remove_conflicting_links(match: re.Match[str]) -> str:
        attributes = {
            item.group("name").casefold(): html.unescape(item.group("value")).strip()
            for item in _TAG_ATTRIBUTE_RE.finditer(match.group(0))
        }
        rel_values = {
            item.casefold() for item in attributes.get("rel", "").split()
        }
        return "" if rel_values.intersection({"canonical", "alternate"}) else match.group(0)

    head_content = _LINK_TAG_RE.sub(remove_conflicting_links, head_content)
    head_content += _localized_seo_head_markup(path=path, locale=locale)
    return (
        document[: head_match.start("content")]
        + head_content
        + document[head_match.end("content") :]
    )


def _is_propertyquarry_document(document: str) -> bool:
    head_match = _HEAD_CONTENT_RE.search(str(document)[:131_072])
    if head_match is None:
        return False
    head_content = _PROTECTED_BLOCK_RE.sub("", head_match.group("content"))
    for meta_tag in _META_TAG_RE.findall(head_content):
        attributes = {
            match.group("name").casefold(): html.unescape(match.group("value")).strip()
            for match in _TAG_ATTRIBUTE_RE.finditer(meta_tag)
        }
        if (
            attributes.get("name", "").casefold() == "application-name"
            and attributes.get("content", "").casefold() == "propertyquarry"
        ):
            return True
    return False


def _status_copy(locale: str) -> tuple[str, str, str]:
    if locale == PROPERTYQUARRY_PSEUDO_LOCALE:
        return tuple(_pseudo_localize(item) for item in _STATUS_COPY["en"])  # type: ignore[return-value]
    return _STATUS_COPY[locale]


def _head_markup(
    *,
    locale: str,
) -> str:
    return (
        '<meta data-pq-localization-head name="propertyquarry:translation-status" '
        'content="global-route-shell; english-fallback-unreviewed-legal-provider-customer-content; independent-native-review-required">'
        '<link rel="stylesheet" href="/static/propertyquarry-localization.css">'
        f'<meta name="propertyquarry:locale" content="{html.escape(locale, quote=True)}">'
    )


def _selector_markup(
    *,
    path: str,
    query_string: bytes | str,
    locale: str,
    placement: str,
) -> str:
    heading, fallback_status, review_status = _status_copy(locale)
    current_label = _LOCALE_LABELS.get(locale, "Pseudo locale")
    options: list[str] = []
    for target_locale in PROPERTYQUARRY_PUBLIC_LOCALES:
        href = _route_url(path=path, query_string=query_string, locale=target_locale)
        current = ' aria-current="true"' if target_locale == locale else ""
        options.append(
            f'<a href="{html.escape(href, quote=True)}" hreflang="{target_locale}" lang="{target_locale}"{current}>'
            f'{html.escape(_LOCALE_LABELS[target_locale])}</a>'
        )
    return (
        f'<aside class="pq-locale-panel" data-pq-localization-status data-pq-locale="{html.escape(locale, quote=True)}" '
        f'data-pq-localization-placement="{html.escape(placement, quote=True)}" '
        'data-pq-localization-coverage="global-required-route-shell" '
        'data-pq-english-fallback="unreviewed-legal provider-specific customer-or-listing-content" '
        'data-pq-professional-review="false" role="note" '
        f'aria-label="{html.escape(heading, quote=True)}">'
        '<details class="pq-locale-disclosure">'
        f'<summary aria-label="{html.escape(f"{heading}: {current_label}", quote=True)}">'
        '<span class="pq-locale-glyph" aria-hidden="true">A/文</span>'
        f'<span class="pq-locale-current" lang="{html.escape(locale, quote=True)}">{html.escape(current_label)}</span>'
        '</summary>'
        '<div class="pq-locale-menu">'
        f'<strong>{html.escape(heading)}</strong>'
        f'<nav class="pq-locale-options" data-pq-locale-selector aria-label="{html.escape(heading, quote=True)}">'
        f'{"<span aria-hidden=\"true\"> · </span>".join(options)}</nav>'
        f'<p>{html.escape(fallback_status)} {html.escape(review_status)}</p>'
        '</div></details>'
        '</aside>'
    )


def localize_propertyquarry_html(
    document: str,
    *,
    locale: str,
    path: str,
    query_string: bytes | str = b"",
    preserve_locale_in_urls: bool = True,
    include_localized_seo: bool = True,
) -> str:
    normalized_locale = normalize_propertyquarry_locale(locale, allow_pseudo=True)
    if normalized_locale is None:
        raise ValueError("unsupported_propertyquarry_locale")
    translated_path = propertyquarry_route_is_translated(path)
    if not translated_path:
        return document
    parts = _PROTECTED_BLOCK_RE.split(str(document))
    localized_parts = [
        part
        if index % 2
        else _localize_unprotected_html(
            part,
            locale=normalized_locale,
            preserve_locale_in_urls=preserve_locale_in_urls,
        )
        for index, part in enumerate(parts)
    ]
    localized = _set_html_lang("".join(localized_parts), normalized_locale)
    if include_localized_seo and normalized_locale in PROPERTYQUARRY_PUBLIC_LOCALES:
        localized = _apply_localized_seo(
            localized,
            path=str(path or "").rstrip("/") or "/",
            locale=normalized_locale,
        )
    if "data-pq-localization-head" not in localized:
        localized = re.sub(
            r"</head\s*>",
            f"{_head_markup(locale=normalized_locale)}</head>",
            localized,
            count=1,
            flags=re.IGNORECASE,
        )
    if "data-pq-localization-status" not in localized:
        if _LOCALIZATION_SLOT in localized:
            localized = localized.replace(
                _LOCALIZATION_SLOT,
                _selector_markup(
                    path=path,
                    query_string=query_string,
                    locale=normalized_locale,
                    placement="integrated",
                ),
                1,
            )
        else:
            localized = re.sub(
                r"</body\s*>",
                f"{_selector_markup(path=path, query_string=query_string, locale=normalized_locale, placement='floating')}</body>",
                localized,
                count=1,
                flags=re.IGNORECASE,
            )
    return localized


def _replace_single_header(headers: list[tuple[bytes, bytes]], name: str, value: str) -> list[tuple[bytes, bytes]]:
    encoded_name = name.lower().encode("ascii")
    updated = [(key, item) for key, item in headers if key.lower() != encoded_name]
    updated.append((encoded_name, value.encode("latin-1")))
    return updated


def _drop_headers(headers: list[tuple[bytes, bytes]], *names: str) -> list[tuple[bytes, bytes]]:
    dropped = {name.lower().encode("ascii") for name in names}
    return [(key, value) for key, value in headers if key.lower() not in dropped]


def _append_vary(headers: list[tuple[bytes, bytes]], *names: str) -> list[tuple[bytes, bytes]]:
    existing = _header_value(headers, "vary")
    values = [item.strip() for item in existing.split(",") if item.strip()]
    lowered = {item.casefold() for item in values}
    for name in names:
        if name.casefold() not in lowered:
            values.append(name)
            lowered.add(name.casefold())
    return _replace_single_header(headers, "Vary", ", ".join(values))


def _scope_is_secure(scope: Scope) -> bool:
    return str(scope.get("scheme") or "").strip().casefold() == "https"


def _content_type_is_utf8_html(content_type: str) -> bool:
    parts = [part.strip() for part in str(content_type or "").split(";")]
    if not parts or parts[0].casefold() != "text/html":
        return False
    charsets: list[str] = []
    for parameter in parts[1:]:
        name, separator, value = parameter.partition("=")
        if name.strip().casefold() != "charset":
            continue
        if not separator:
            return False
        charset = value.strip().strip("\"'").casefold().replace("_", "-")
        charsets.append(charset)
    return not charsets or all(charset in {"utf-8", "utf8"} for charset in charsets)


def _content_length_within_limit(headers: list[tuple[bytes, bytes]], limit: int) -> bool:
    raw_length = _header_value(headers, "content-length").strip()
    if not raw_length:
        return True
    if not raw_length.isdecimal():
        return False
    return int(raw_length) <= limit


def _prepare_response_start(
    response_start: Message,
    *,
    decision: PropertyQuarryLocaleDecision,
    secure_cookie: bool,
    content_language: str | None = None,
    vary_by_locale: bool = False,
    localized_representation: bool = False,
) -> Message:
    prepared = dict(response_start)
    headers = list(prepared.get("headers") or [])
    if content_language is not None:
        headers = _replace_single_header(headers, "Content-Language", content_language)
    if vary_by_locale:
        headers = _append_vary(headers, "Cookie", "Accept-Language")
    if localized_representation:
        headers = _replace_single_header(
            headers,
            "X-PropertyQuarry-Translation-Status",
            "global-route-shell; english-fallback-unreviewed-legal-provider-customer-content; independent-native-review-required",
        )
    else:
        headers = _drop_headers(headers, "x-propertyquarry-translation-status")
    if decision.query_locale_valid:
        headers.append(
            (
                b"set-cookie",
                propertyquarry_locale_cookie_header(
                    decision.locale,
                    secure=secure_cookie,
                ).encode("latin-1"),
            )
        )
    prepared["headers"] = headers
    return prepared


class PropertyQuarryLocalizationMiddleware:
    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        max_html_bytes: int = PROPERTYQUARRY_MAX_LOCALIZED_HTML_BYTES,
    ) -> None:
        self.app = app
        configured_limit = int(max_html_bytes)
        if configured_limit < 1:
            raise ValueError("max_html_bytes_must_be_positive")
        self.max_html_bytes = min(configured_limit, PROPERTYQUARRY_MAX_LOCALIZED_HTML_BYTES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or str(scope.get("method") or "GET").upper() not in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not propertyquarry_route_is_translated(path):
            await self.app(scope, receive, send)
            return

        request_headers = list(scope.get("headers") or [])
        query_string = scope.get("query_string") or b""
        decision = resolve_propertyquarry_locale(query_string=query_string, headers=request_headers)
        # Query selection is the only case that needs one-hop URL propagation.
        # Cookie and Accept-Language decisions naturally survive navigation and
        # keeping them out of every internal URL avoids leaking stale locale
        # state into copied links and canonical application routes.
        preserve_locale = decision.source == "query"
        method = str(scope.get("method") or "GET").upper()
        secure_cookie = _scope_is_secure(scope)
        pending_start: Message | None = None
        buffered_body: list[Message] = []
        buffered_size = 0
        passthrough = False

        async def flush_source_response(*, propertyquarry_document: bool = False) -> None:
            nonlocal pending_start, buffered_body, buffered_size, passthrough
            if pending_start is None:
                return
            source_start = pending_start
            if propertyquarry_document:
                source_start = _prepare_response_start(
                    source_start,
                    decision=decision,
                    secure_cookie=secure_cookie,
                    content_language="en",
                )
            await send(source_start)
            for buffered_message in buffered_body:
                await send(buffered_message)
            pending_start = None
            buffered_body = []
            buffered_size = 0
            passthrough = True

        async def localized_send(message: Message) -> None:
            nonlocal pending_start, buffered_body, buffered_size, passthrough
            message_type = message.get("type")
            if message_type == "http.response.start":
                status = int(message.get("status") or 200)
                headers = list(message.get("headers") or [])
                content_type = _header_value(headers, "content-type")
                content_encoding = _header_value(headers, "content-encoding").strip()
                redirect_response = 300 <= status < 400

                if redirect_response:
                    await send(message)
                    passthrough = True
                    return

                inspectable_html = (
                    _content_type_is_utf8_html(content_type)
                    and not content_encoding
                    and _content_length_within_limit(headers, self.max_html_bytes)
                )
                if inspectable_html and method == "GET":
                    pending_start = dict(message)
                    return

                await send(message)
                passthrough = True
                return

            if passthrough or pending_start is None:
                await send(message)
                return

            if message_type != "http.response.body":
                await flush_source_response()
                await send(message)
                return

            buffered_message = dict(message)
            buffered_message["body"] = bytes(message.get("body") or b"")
            buffered_body.append(buffered_message)
            buffered_size += len(buffered_message["body"])
            if buffered_size > self.max_html_bytes:
                await flush_source_response()
                return
            if message.get("more_body"):
                return

            body = b"".join(
                bytes(buffered_message.get("body") or b"")
                for buffered_message in buffered_body
            )
            try:
                source = body.decode("utf-8")
            except UnicodeDecodeError:
                await flush_source_response()
                return

            propertyquarry_document = _is_propertyquarry_document(source)
            if not propertyquarry_document:
                await flush_source_response()
                return

            status = int(pending_start.get("status") or 200)
            localized_body = localize_propertyquarry_html(
                source,
                locale=decision.locale,
                path=path,
                query_string=query_string,
                preserve_locale_in_urls=preserve_locale,
                include_localized_seo=200 <= status < 300,
            ).encode("utf-8")
            if len(localized_body) > self.max_html_bytes:
                await flush_source_response(propertyquarry_document=True)
                return

            localized_start = dict(pending_start)
            localized_headers = _drop_headers(
                list(localized_start.get("headers") or []),
                "content-length",
                "etag",
                "content-md5",
            )
            localized_headers = _replace_single_header(
                localized_headers,
                "Content-Length",
                str(len(localized_body)),
            )
            localized_start["headers"] = localized_headers
            await send(
                _prepare_response_start(
                    localized_start,
                    decision=decision,
                    secure_cookie=secure_cookie,
                    content_language=decision.locale,
                    vary_by_locale=True,
                    localized_representation=True,
                )
            )
            await send({"type": "http.response.body", "body": localized_body, "more_body": False})
            pending_start = None
            buffered_body = []
            buffered_size = 0
            passthrough = True

        await self.app(scope, receive, localized_send)
        if pending_start is not None:
            await flush_source_response()

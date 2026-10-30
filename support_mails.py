"""
ManagerPresence - Mails de support envoyés par le serveur (Brevo)

Avant : l'app envoyait elle-même ces mails par SMTP Gmail, avec le mot de passe d'application
de cp.support.dev écrit dans l'APK (lisible par décompilation). Désormais l'app appelle le
serveur, qui seul détient la clé Brevo.

- POST /support/message : rapport de plantage ou message du formulaire de support,
  toujours vers l'adresse support (destinataire fixe).
- POST /support/notif-change : prévient l'ancienne et la nouvelle adresse de notification
  d'une structure (texte fixé ici, champs sans lien, débit limité).
"""

import json
import re
import threading
import time
import urllib.request

from flask import Blueprint, jsonify, request

SUPPORT_EMAIL = "cp.support.dev@gmail.com"

_RE_EMAIL = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}$")
_RE_INTERDIT = re.compile(r"https?:|www\.|://|[<>]", re.IGNORECASE)


def _propre(valeur, maxi=80):
    """Texte inséré dans un mail envoyé à un tiers : une ligne, court, sans lien ni balise."""
    v = re.sub(r"[\x00-\x1f\x7f]", " ", str(valeur or "")).strip()[:maxi]
    return "" if _RE_INTERDIT.search(v) else v


class _Debit:
    """Limite simple en mémoire : n événements par fenêtre glissante, par clé."""

    def __init__(self, n, fenetre):
        self.n, self.fenetre, self.traces, self.verrou = n, fenetre, {}, threading.Lock()

    def accepte(self, cle):
        maintenant = time.time()
        with self.verrou:
            t = [x for x in self.traces.get(cle, []) if x > maintenant - self.fenetre]
            if len(t) >= self.n:
                self.traces[cle] = t
                return False
            t.append(maintenant)
            self.traces[cle] = t
            return True


def creer_blueprint(brevo_api_key):
    bp = Blueprint("support_mails", __name__)
    debit_support = _Debit(20, 3600)        # par IP : 20 messages/heure
    debit_notif_ip = _Debit(5, 3600)        # par IP : 5 changements d'adresse/heure
    debit_notif_global = _Debit(200, 86400)  # tout le serveur : 200/jour

    def ip():
        return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or request.remote_addr or "?")

    def brevo(destinataire, sujet, texte, nom_expediteur="ManagerPresence Support", reply_to=""):
        if not brevo_api_key:
            print(f"[SUPPORT] BREVO_API_KEY manquant — {sujet}")
            return False
        corps = {
            "sender": {"name": nom_expediteur, "email": SUPPORT_EMAIL},
            "to": [{"email": destinataire}],
            "subject": sujet,
            "textContent": texte,
        }
        if reply_to:
            corps["replyTo"] = {"email": reply_to}
        try:
            req = urllib.request.Request(
                "https://api.brevo.com/v3/smtp/email",
                data=json.dumps(corps).encode("utf-8"),
                headers={"api-key": brevo_api_key, "Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20):
                return True
        except Exception as e:
            print(f"[SUPPORT] ❌ Brevo: {e}")
            return False

    @bp.route("/support/message", methods=["POST"])
    def support_message():
        if not debit_support.accepte(ip()):
            return jsonify({"erreur": "trop_de_messages"}), 429
        d = request.get_json(silent=True) or {}
        sujet = re.sub(r"[\r\n]+", " ", str(d.get("sujet", "")))[:150].strip() or "Message"
        texte = str(d.get("corps", ""))[:60000]
        if not texte.strip():
            return jsonify({"erreur": "vide"}), 400
        reponse = str(d.get("email_reponse", "")).strip()
        ok = brevo(SUPPORT_EMAIL, f"[ManagerPresence] {sujet}", texte,
                   reply_to=reponse if _RE_EMAIL.match(reponse) else "")
        return (jsonify({"ok": True}), 200) if ok else (jsonify({"erreur": "envoi"}), 502)

    @bp.route("/support/notif-change", methods=["POST"])
    def notif_change():
        d = request.get_json(silent=True) or {}
        club = _propre(d.get("club", "")) or "votre structure"
        cadre = _propre(d.get("cadre", ""), 60) or "?"
        ancien = str(d.get("ancien", "")).strip()
        nouveau = str(d.get("nouveau", "")).strip()
        if not _RE_EMAIL.match(nouveau) or (ancien and not _RE_EMAIL.match(ancien)):
            return jsonify({"erreur": "adresse"}), 400
        if not debit_notif_ip.accepte(ip()) or not debit_notif_global.accepte("tous"):
            return jsonify({"erreur": "trop_de_messages"}), 429
        date = time.strftime("%d/%m/%Y à %H:%M", time.localtime())
        signature = f"\n---\nManagerPresence — {club}\n"
        ok_ancien = True
        if ancien and ancien.lower() != nouveau.lower():
            ok_ancien = brevo(ancien, f"⚠️ Changement d'adresse de notification - {club}",
                              "Bonjour,\n\n"
                              f"Vous n'êtes plus le destinataire des notifications de l'application ManagerPresence — {club}.\n\n"
                              f"📅 Date : {date}\n👤 Modifié par : {cadre}\n📧 Nouveau destinataire : {nouveau}\n\n"
                              "Si vous n'êtes pas à l'origine de ce changement, contactez votre administrateur.\n" + signature,
                              nom_expediteur=club)
        ok_nouveau = brevo(nouveau, f"✅ Vous êtes maintenant le destinataire des notifications - {club}",
                           "Bonjour,\n\n"
                           f"Cette adresse est maintenant configurée pour recevoir les notifications de l'application ManagerPresence — {club}.\n\n"
                           f"📅 Date : {date}\n👤 Configuré par : {cadre}\n\n"
                           "Vous recevrez sur cette adresse les notifications administratives liées à la structure.\n" + signature,
                           nom_expediteur=club)
        return jsonify({"ancien": ok_ancien, "nouveau": ok_nouveau})

    return bp

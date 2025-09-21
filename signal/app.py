# signal/app.py
#
# Serveur de signalisation WebSocket pour jeux Godot (multi-projets).
# - Gère des "rooms" N-Joueurs, *par jeu* (namespace), avec hôte = créateur.
# - Rooms publiques (sans mot de passe) ou privées (mot de passe requis).
# - Capacité limitée (par défaut et borne max configurables).
# - Relais OFFER/ANSWER/ICE (WebRTC) entre pairs.
# - Listing des rooms joignables par jeu (HTTP GET /rooms et WS "list").
#
# ⚠️ Ce serveur ne transporte pas de trafic de jeu : il ne fait que la "signalisation".
#     Les paquets de jeu passent en P2P (STUN) ou via TURN si nécessaire.

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Set, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import PlainTextResponse, JSONResponse

# -----------------------------------------------------------------------------
# Configuration (via variables d'environnement)
# -----------------------------------------------------------------------------
# SEL pour HMAC (hash des mots de passe de room). CHANGE-MOI en prod.
SECRET_SALT = os.getenv("SIGNAL_SALT", "change-me-salt").encode()

# Capacité par défaut des rooms et capacité max autorisée.
DEFAULT_CAPACITY = int(os.getenv("DEFAULT_CAPACITY", "8"))
MAX_CAPACITY = int(os.getenv("MAX_CAPACITY", "16"))

# Longueurs max pour "game", "room", "name"
MAX_LEN_GAME = int(os.getenv("MAX_LEN_GAME", "64"))
MAX_LEN_ROOM = int(os.getenv("MAX_LEN_ROOM", "64"))
MAX_LEN_NAME = int(os.getenv("MAX_LEN_NAME", "64"))

# -----------------------------------------------------------------------------
# Modèles en mémoire (RAM)
# -----------------------------------------------------------------------------
@dataclass
class Peer:
    ws: WebSocket
    name: str


@dataclass
class Room:
    game: str
    name: str
    private: bool
    capacity: int
    pwd_hash: Optional[str] = None
    host_id: Optional[int] = None
    peers: Dict[int, Peer] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def size(self) -> int:
        return len(self.peers)

    def is_full(self) -> bool:
        return self.size >= self.capacity


# Index principal: (game, room_name) -> Room
rooms: Dict[Tuple[str, str], Room] = {}

# Pour lister rapidement par jeu: game -> set(room_name)
game_index: Dict[str, Set[str]] = {}

# Générateur d'IDs peers
peer_id_seq = itertools.count(1)

# -----------------------------------------------------------------------------
# Utilitaires
# -----------------------------------------------------------------------------
def _h(s: str) -> str:
    """Hash HMAC-SHA256 (avec SECRET_SALT) d'une chaîne (mot de passe)."""
    return hmac.new(SECRET_SALT, s.encode(), hashlib.sha256).hexdigest()


def verify_pwd(stored_hash: Optional[str], pwd_try: str) -> bool:
    if not stored_hash:
        return False
    return hmac.compare_digest(stored_hash, _h(pwd_try))


def clamp_int(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def clean_str(s: str, max_len: int) -> str:
    """Nettoyage minimal: cast str, strip, limite de longueur."""
    if s is None:
        return ""
    s = str(s).strip()
    if len(s) > max_len:
        s = s[:max_len]
    return s


def room_key(game: str, room: str) -> Tuple[str, str]:
    return (game, room)


def upsert_room_index(r: Room) -> None:
    if r.game not in game_index:
        game_index[r.game] = set()
    game_index[r.game].add(r.name)


def drop_room_index(r: Room) -> None:
    gset = game_index.get(r.game)
    if not gset:
        return
    gset.discard(r.name)
    if not gset:
        game_index.pop(r.game, None)


def room_public_summary(r: Room) -> Dict:
    """Résumé 'safe' pour retourner aux clients (liste rooms)."""
    return {
        "game": r.game,
        "room": r.name,
        "private": r.private,     # True = mot de passe requis
        "capacity": r.capacity,
        "players": r.size,
        "host_id": r.host_id,     # identifiant logique (pas PII)
        "created_at": int(r.created_at),
        "full": r.is_full(),
    }


def peers_brief(r: Room, exclude_id: Optional[int] = None) -> List[Dict]:
    out = []
    for pid, p in r.peers.items():
        if exclude_id is not None and pid == exclude_id:
            continue
        out.append({"id": pid, "name": p.name})
    return out


# -----------------------------------------------------------------------------
# FastAPI app & endpoints HTTP
# -----------------------------------------------------------------------------
app = FastAPI(title="Godot Signaling Relay", version="1.1.0")


@app.get("/", response_class=PlainTextResponse)
def health() -> str:
    """Healthcheck simple (utile pour NGINX / monitoring)."""
    return "OK\n"


@app.get("/rooms")
def list_rooms_http(
    game: str = Query(..., description="Identifiant du jeu"),
    only_public: bool = Query(False, description="Si vrai, n'inclut que les rooms publiques"),
    not_full: bool = Query(True, description="Si vrai, exclut les rooms pleines"),
) -> JSONResponse:
    """Retourne la liste des rooms joignables pour un jeu donné (HTTP).
    On n'expose JAMAIS les mots de passe, seulement des meta 'safe'.
    """
    g = clean_str(game, MAX_LEN_GAME)
    rnames = list(game_index.get(g, set()))
    res = []
    for rn in rnames:
        rk = room_key(g, rn)
        r = rooms.get(rk)
        if not r:
            continue
        if only_public and r.private:
            continue
        if not_full and r.is_full():
            continue
        res.append(room_public_summary(r))
    return JSONResponse({"game": g, "rooms": sorted(res, key=lambda x: (x["full"], x["created_at"]))})


# -----------------------------------------------------------------------------
# Protocole WebSocket
#   - "create": créer une room (host = créateur)
#   - "join":   rejoindre une room
#   - "list":   lister rooms pour un jeu
#   - "offer"/"answer"/"ice": relais de signalisation
#   - "leave":  quitter proprement
#
# Messages de réponse (exemples):
#   - "hello":   infos room + peers
#   - "rooms":   liste des rooms (réponse à "list")
#   - "peer_joined"/"peer_left"/"new_host"
#   - "error":   erreur bloquante (socket souvent fermé ensuite)
# -----------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    # Contexte de la connexion courante
    my_peer_id: Optional[int] = None
    my_game: Optional[str] = None
    my_room: Optional[str] = None

    try:
        # Le premier message DOIT être "create" ou "join" ou "list"
        first_raw = await ws.receive_text()
        first = json.loads(first_raw)
        ftype = first.get("type")

        if ftype not in ("create", "join", "list"):
            await _send_error(ws, "first message must be 'create' | 'join' | 'list'")
            await ws.close()
            return

        # ---------------------------------------------------------------------
        # LIST (permet de lister via WS sans ouvrir un endpoint HTTP séparé)
        # ---------------------------------------------------------------------
        if ftype == "list":
            game = clean_str(first.get("game", ""), MAX_LEN_GAME)
            only_public = bool(first.get("only_public", False))
            not_full = bool(first.get("not_full", True))
            payload = _list_rooms_payload(game, only_public, not_full)
            await ws.send_text(json.dumps(payload))
            # On ferme la socket car c'est un one-shot de listing (simple).
            await ws.close()
            return

        # ---------------------------------------------------------------------
        # CREATE / JOIN
        # ---------------------------------------------------------------------
        game = clean_str(first.get("game", ""), MAX_LEN_GAME)
        room = clean_str(first.get("room", ""), MAX_LEN_ROOM)
        name = clean_str(first.get("name", "anon"), MAX_LEN_NAME)
        if not game or not room:
            await _send_error(ws, "missing 'game' or 'room'")
            await ws.close()
            return

        # Préparation de la room selon le type
        rk = room_key(game, room)

        if ftype == "create":
            # Attributs optionnels
            capacity = clamp_int(int(first.get("capacity", DEFAULT_CAPACITY)), 1, MAX_CAPACITY)
            private = bool(first.get("private", False))
            pwd = first.get("pwd")  # None ou str

            # Sémantique: room privée => mot de passe OBLIGATOIRE
            if private and not pwd:
                await _send_error(ws, "private room requires 'pwd'")
                await ws.close()
                return

            existing = rooms.get(rk)
            if existing and existing.size > 0:
                await _send_error(ws, "room already exists")
                await ws.close()
                return

            # (Ré)initialiser la room
            r = Room(
                game=game,
                name=room,
                private=private if private else False,
                capacity=capacity,
                pwd_hash=_h(pwd) if (private and pwd) else None,
            )
            rooms[rk] = r
            upsert_room_index(r)

        else:  # ftype == "join"
            r = rooms.get(rk)
            if not r:
                await _send_error(ws, "room not found")
                await ws.close()
                return
            if r.is_full():
                await _send_error(ws, "room is full")
                await ws.close()
                return
            # Room privée -> vérifier le mot de passe
            if r.private:
                pwd = first.get("pwd")
                if not pwd or not verify_pwd(r.pwd_hash, pwd):
                    await _send_error(ws, "invalid password")
                    await ws.close()
                    return

        # À partir d'ici, on a une Room r valide (créée ou existante)
        my_peer_id = next(peer_id_seq)
        r.peers[my_peer_id] = Peer(ws=ws, name=name)
        if r.host_id is None:
            r.host_id = my_peer_id  # créateur = host (création) ou 1er entrant si room vierge

        my_game, my_room = game, room

        # Réponse "hello" au nouvel arrivant
        await ws.send_text(json.dumps({
            "type": "hello",
            "game": r.game,
            "room": r.name,
            "your_id": my_peer_id,
            "host_id": r.host_id,
            "private": r.private,
            "capacity": r.capacity,
            "players": r.size,
            "peers": peers_brief(r, exclude_id=my_peer_id),  # [{id,name},...]
        }))

        # Notifier les autres
        await _broadcast(r, {
            "type": "peer_joined",
            "peer_id": my_peer_id,
            "name": name,
            "players": r.size,
        }, exclude={my_peer_id})

        # Boucle principale: router offer/answer/ice ou leave
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype in ("offer", "answer", "ice"):
                to_id = msg.get("to")
                if to_id is None:
                    # message mal formé -> on ignore poliment
                    continue
                await _send_to(r, int(to_id), msg | {"from": my_peer_id})

            elif mtype == "list":
                # Permet à un client déjà connecté de lister (ex: UI in-game)
                game_req = clean_str(msg.get("game", r.game), MAX_LEN_GAME)
                only_public = bool(msg.get("only_public", False))
                not_full = bool(msg.get("not_full", True))
                payload = _list_rooms_payload(game_req, only_public, not_full)
                await ws.send_text(json.dumps(payload))

            elif mtype == "leave":
                await ws.close()
                break

            else:
                # Inconnu -> ignorer (ou logger si besoin)
                pass

    except WebSocketDisconnect:
        # Déconnexion propre côté client
        pass
    except Exception as e:
        # Remonter l'erreur au client si possible
        try:
            await _send_error(ws, str(e))
        except:  # noqa
            pass
    finally:
        # Nettoyage si le peer était attaché à une room
        if my_peer_id and my_game and my_room:
            rk = room_key(my_game, my_room)
            r = rooms.get(rk)
            if r and my_peer_id in r.peers:
                # Retirer le peer
                r.peers.pop(my_peer_id, None)

                # Host parti ? élire le prochain comme host (ou None si room vide)
                if r.host_id == my_peer_id:
                    r.host_id = next(iter(r.peers), None)
                    # Annoncer le nouveau host aux survivants
                    await _broadcast(r, {
                        "type": "new_host",
                        "host_id": r.host_id,
                    })

                # Notifier le départ
                await _broadcast(r, {
                    "type": "peer_left",
                    "peer_id": my_peer_id,
                    "players": r.size,
                })

                # Room vide -> purge des index
                if r.size == 0:
                    rooms.pop(rk, None)
                    drop_room_index(r)


# -----------------------------------------------------------------------------
# Helpers WS
# -----------------------------------------------------------------------------
async def _send_error(ws: WebSocket, detail: str):
    """Envoi d'un message d'erreur standardisé."""
    await ws.send_text(json.dumps({"type": "error", "detail": detail}))


def _list_rooms_payload(game: str, only_public: bool, not_full: bool) -> Dict:
    g = clean_str(game, MAX_LEN_GAME)
    rnames = list(game_index.get(g, set()))
    out = []
    for rn in rnames:
        rk = room_key(g, rn)
        r = rooms.get(rk)
        if not r:
            continue
        if only_public and r.private:
            continue
        if not_full and r.is_full():
            continue
        out.append(room_public_summary(r))
    return {"type": "rooms", "game": g, "rooms": sorted(out, key=lambda x: (x["full"], x["created_at"]))}


async def _broadcast(r: Room, payload: Dict, exclude: Optional[Set[int]] = None):
    """Envoi à tous les peers de la room (sauf 'exclude'). Nettoie les morts."""
    if exclude is None:
        exclude = set()
    dead = []
    for pid, peer in r.peers.items():
        if pid in exclude:
            continue
        try:
            await peer.ws.send_text(json.dumps(payload))
        except:  # client mort
            dead.append(pid)
    for pid in dead:
        r.peers.pop(pid, None)


async def _send_to(r: Room, peer_id: int, payload: Dict):
    """Envoi à un pair donné s'il existe."""
    p = r.peers.get(peer_id)
    if p:
        await p.ws.send_text(json.dumps(payload))

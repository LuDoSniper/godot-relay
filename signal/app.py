# signal/app.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from typing import Dict, Set, Optional
import json
import itertools

app = FastAPI(title="Godot Signaling")

# Mémoire en RAM (simple): rooms[room_id] = {"peers": {peer_id: websocket}, "host_id": Optional[int]}
rooms: Dict[str, Dict] = {}
peer_id_seq = itertools.count(1)  # génère 1,2,3,...

@app.get("/", response_class=PlainTextResponse)
def health():
    return "OK\n"

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """
    Protocole de message JSON (minimal) attendu côté client:
    1) JOIN:   {"type":"join","room":"<room-id>","name":"<optional>"}
    2) OFFER:  {"type":"offer","to":<peer_id>,"sdp":"..."}
    3) ANSWER: {"type":"answer","to":<peer_id>,"sdp":"..."}
    4) ICE:    {"type":"ice","to":<peer_id>,"candidate":"...","sdpMid":"...","sdpMLineIndex":<int>}
    5) LEAVE:  {"type":"leave"}
    Le serveur relaie vers le destinataire sous la forme: {..., "from": <emitter_peer_id>}
    """
    await ws.accept()  # upgrade HTTP -> WebSocket

    peer_id: Optional[int] = None
    room_id: Optional[str] = None

    try:
        # 1) On attend le JOIN initial pour savoir dans quelle room te placer
        join_raw = await ws.receive_text()
        join_msg = json.loads(join_raw)
        if join_msg.get("type") != "join":
            await ws.send_text(json.dumps({"type": "error", "detail": "first message must be 'join'"}))
            await ws.close()
            return

        room_id = str(join_msg["room"])
        name = str(join_msg.get("name", "anon"))

        # Crée la room si besoin, puis enregistre ce websocket comme nouveau peer
        if room_id not in rooms:
            rooms[room_id] = {"peers": {}, "host_id": None}
        room = rooms[room_id]

        peer_id = next(peer_id_seq)
        room["peers"][peer_id] = ws

        # Le premier connecté devient "host"
        if room["host_id"] is None:
            room["host_id"] = peer_id

        # 2) Réponse "hello" au nouvel arrivant avec son id, l'id de l'hôte et la liste des pairs déjà présents
        await ws.send_text(json.dumps({
            "type": "hello",
            "your_id": peer_id,
            "host_id": room["host_id"],
            "peers": [pid for pid in room["peers"] if pid != peer_id],
        }))

        # 3) Annonce aux autres qu'un nouveau peer arrive (utile pour UI ou logs client)
        await broadcast(room_id, {
            "type": "peer_joined",
            "peer_id": peer_id,
            "name": name,
        }, exclude={peer_id})

        # 4) Boucle principale: reçoit des messages et les route
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind in ("offer", "answer", "ice"):
                to_id = msg.get("to")
                if to_id is None:
                    continue  # message mal formé, on ignore
                await send_to(room_id, int(to_id), msg | {"from": peer_id})
            elif kind == "leave":
                # Le client veut quitter proprement
                await ws.close()
                break
            else:
                # Messages inconnus -> ignore (ou logger si tu veux)
                pass

    except WebSocketDisconnect:
        # Déconnexion propre du client (fermeture du socket)
        pass
    except Exception as e:
        # Erreur côté serveur -> on essaie d'informer le client avant de fermer
        try:
            await ws.send_text(json.dumps({"type": "error", "detail": str(e)}))
        except:  # noqa
            pass
    finally:
        # 5) Nettoyage: retirer le peer de la room, élire nouvel hôte si besoin, notifier les autres
        if room_id and peer_id:
            room = rooms.get(room_id)
            if room and peer_id in room["peers"]:
                room["peers"].pop(peer_id, None)

                # Host parti ? élire le prochain (ou None si room vide)
                if room["host_id"] == peer_id:
                    room["host_id"] = next(iter(room["peers"]), None)
                    await broadcast(room_id, {
                        "type": "new_host",
                        "host_id": room["host_id"],
                    })

                # Notifier les autres du départ
                await broadcast(room_id, {
                    "type": "peer_left",
                    "peer_id": peer_id,
                })

                # Room vide ? la supprimer
                if not room["peers"]:
                    rooms.pop(room_id, None)

async def broadcast(room_id: str, payload: dict, exclude: Set[int] = set()):
    """Envoie 'payload' à tous les peers de la room (sauf 'exclude')."""
    room = rooms.get(room_id)
    if not room:
        return
    dead = []
    for pid, w in room["peers"].items():
        if pid in exclude:
            continue
        try:
            await w.send_text(json.dumps(payload))
        except:  # client mort
            dead.append(pid)
    for pid in dead:
        room["peers"].pop(pid, None)

async def send_to(room_id: str, peer_id: int, payload: dict):
    """Envoie 'payload' au peer 'peer_id' s'il est dans la room."""
    room = rooms.get(room_id, {})
    ws = room.get("peers", {}).get(peer_id)
    if ws:
        await ws.send_text(json.dumps(payload))

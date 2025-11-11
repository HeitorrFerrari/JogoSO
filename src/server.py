# server.py
import socket
import threading
import json
import time

HOST = 'localhost'
PORT = 5050

LOCK = threading.Lock()
MAX_PLAYERS = 2
BOARD_SIZE = 5  # 5x5
TANKS_PER_PLAYER = 3
ROWS = ['A','B','C','D','E']
COLS = ['1','2','3','4','5']

def valid_pos(pos):
    if len(pos) != 2: return False
    r, c = pos[0].upper(), pos[1]
    return r in ROWS and c in COLS

class PlayerState:
    def __init__(self, conn, addr, name, pid):
        self.conn = conn
        self.addr = addr
        self.name = name
        self.id = pid  # 1 or 2
        # board: dict pos -> 'T' (tank), 'H' (hit), 'M' (miss')
        self.board = {r+c: ' ' for r in ROWS for c in COLS}
        self.tanks_left = 0
        self.ready = False

    def send(self, obj):
        try:
            raw = json.dumps(obj).encode()
            self.conn.sendall(raw)
        except Exception as e:
            print(f"Erro enviando para {self.name}: {e}")

class BattleServer:
    def __init__(self, host, port):
        self.players = []
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind((host, port))
        self.server.listen(MAX_PLAYERS)
        self.current_turn = 1  # player id who has the turn
        self.running = True

    def broadcast(self, obj):
        for p in self.players:
            p.send(obj)

    def masked_view(self, viewer_id):
        # returns dicts to send to viewer: own board (full) and opponent view (only H/M shown)
        viewer = next(p for p in self.players if p.id == viewer_id)
        opp = next(p for p in self.players if p.id != viewer_id)
        own = viewer.board.copy()
        opp_view = {}
        for pos, val in opp.board.items():
            if val == 'H' or val == 'M':
                opp_view[pos] = val
            else:
                opp_view[pos] = ' '  # hide tanks
        return own, opp_view

    def handle_client(self, player: PlayerState):
        conn = player.conn
        try:
            # Send assign info
            player.send({"type":"ASSIGN","id":player.id, "msg":f"Você é Jogador{player.id} ({player.name})"})
            # Ask for placement
            player.send({"type":"REQUEST_PLACE", "msg":f"Coloque {TANKS_PER_PLAYER} tanques (ex: A1,A3,E5). Linhas A-E, Colunas 1-5."})

            # wait for placement
            data = conn.recv(4096)
            if not data:
                return
            try:
                obj = json.loads(data.decode())
            except:
                player.send({"type":"ERROR","msg":"Formato inválido de mensagem."})
                return

            if obj.get("type") != "PLACEMENT":
                player.send({"type":"ERROR","msg":"Esperado PLACEMENT."})
                return

            positions = obj.get("positions", [])
            # validate
            if len(positions) != TANKS_PER_PLAYER:
                player.send({"type":"ERROR","msg":f"Envie exatamente {TANKS_PER_PLAYER} posições."})
                return

            for pos in positions:
                pos = pos.upper()
                if not valid_pos(pos):
                    player.send({"type":"ERROR","msg":f"Posição inválida: {pos}"})
                    return
                if player.board[pos] == 'T':
                    player.send({"type":"ERROR","msg":f"Posição duplicada: {pos}"})
                    return
                player.board[pos] = 'T'
            player.tanks_left = TANKS_PER_PLAYER
            player.ready = True
            player.send({"type":"PLACED","msg":"Posições registradas com sucesso."})
            print(f"{player.name} colocou: {positions}")

            # Wait until both ready
            while not all(p.ready for p in self.players):
                time.sleep(0.2)

            # Start game loop for this client: listens for ATTACK messages
            while self.running:
                data = conn.recv(4096)
                if not data:
                    break
                try:
                    obj = json.loads(data.decode())
                except:
                    continue

                # Process attack only if it's ATTACK and it's the player's turn
                if obj.get("type") == "ATTACK":
                    with LOCK:
                        if self.current_turn != player.id:
                            player.send({"type":"ERROR","msg":"Não é sua vez."})
                            continue
                        target = obj.get("pos","").upper()
                        if not valid_pos(target):
                            player.send({"type":"ERROR","msg":"Posição inválida."})
                            continue
                        # resolve attack on opponent
                        opponent = next(p for p in self.players if p.id != player.id)
                        cell = opponent.board.get(target)
                        if cell == 'T':
                            opponent.board[target] = 'H'
                            opponent.tanks_left -= 1
                            result = "HIT"
                            print(f"{player.name} acertou {target} em {opponent.name}")
                        elif cell == 'H' or cell == 'M':
                            result = "ALREADY"
                        else:
                            opponent.board[target] = 'M'
                            result = "MISS"
                            print(f"{player.name} errou {target}")
                        # notify attacker of result
                        player.send({"type":"RESULT","result":result,"pos":target})
                        # update both players with masked boards
                        for p in self.players:
                            own, opp_view = self.masked_view(p.id)
                            p.send({"type":"UPDATE","own":own,"opponent_view":opp_view,
                                    "scores":{pl.name:pl.tanks_left for pl in self.players}})
                        # check win
                        if opponent.tanks_left <= 0:
                            self.broadcast({"type":"GAME_OVER","winner":player.name,"msg":f"{player.name} venceu!"})
                            self.running = False
                            break
                        # change turn (only switch on MISS or HIT but not ALREADY)
                        if result != "ALREADY":
                            self.current_turn = opponent.id
                            # notify whose turn
                            next_p = next(p for p in self.players if p.id == self.current_turn)
                            next_p.send({"type":"YOUR_TURN","msg":"É sua vez de atacar."})
                # handle other message types as needed (ping, disconnect, etc.)
        except Exception as e:
            print(f"Erro no handler do jogador {player.name}: {e}")
        finally:
            try:
                conn.close()
            except:
                pass

    def start(self):
        print("Servidor aguardando conexões...")
        while len(self.players) < MAX_PLAYERS:
            conn, addr = self.server.accept()
            # ask name
            conn.sendall(json.dumps({"type":"HELLO","msg":"Envie seu nome: (ex: Alice)"}).encode())
            data = conn.recv(4096)
            try:
                obj = json.loads(data.decode())
                name = obj.get("name","Anon")
            except:
                name = "Anon"
            pid = len(self.players)+1
            player = PlayerState(conn, addr, name, pid)
            self.players.append(player)
            threading.Thread(target=self.handle_client, args=(player,), daemon=True).start()
            print(f"Jogador {pid} conectado: {name} - {addr}")

        # after two connected, notify start and whose turn
        time.sleep(0.5)
        self.broadcast({"type":"START","msg":"Todos conectados. Começando o jogo."})
        # send initial masked boards
        for p in self.players:
            own, opp_view = self.masked_view(p.id)
            p.send({"type":"UPDATE","own":own,"opponent_view":opp_view,
                    "scores":{pl.name:pl.tanks_left for pl in self.players}})
        # give the first turn to player 1
        starter = next(p for p in self.players if p.id == self.current_turn)
        starter.send({"type":"YOUR_TURN","msg":"Você começa. Envie ATTACK com pos (ex: A1)."})
        # keep server alive until game ends
        while self.running:
            time.sleep(0.5)
        print("Jogo finalizado.")
        self.server.close()

if __name__ == "__main__":
    s = BattleServer(HOST, PORT)
    s.start()

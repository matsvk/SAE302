import socket
import json

HOST = "0.0.0.0"
PORT = 5005

# Socket UDP
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((HOST, PORT))

print(f"[*] Serveur UDP démarré sur le port {PORT}. En attente de positions...")

try:
    while True:
        data, addr = sock.recvfrom(2048)
        try:
            message = json.loads(data.decode("utf-8"))
            print(f"[REÇU de {addr[0]}] ID: {message.get('id')} | "
                  f"Lat: {message.get('lat')} | Lon: {message.get('lon')} | "
                  f"Time: {message.get('timestamp')}")
        except json.JSONDecodeError:
            print(f"[ERREUR] Message non-JSON reçu de {addr}")

except KeyboardInterrupt:
    print("\nArrêt du serveur.")
finally:
    sock.close()

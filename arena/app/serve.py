"""Production entrypoint: TLS is mandatory, one worker owns execution state."""
import ipaddress
import os
import uvicorn


def main():
    host = os.environ["ARENA_MANAGEMENT_IP"]
    address = ipaddress.ip_address(host)
    if address.version != 4 or address.is_unspecified or not address.is_private:
        raise ValueError("management must bind an explicit private address")
    game_address = ipaddress.ip_address(os.environ["ARENA_GAME_IP"])
    if game_address.version != 4 or game_address.is_unspecified:
        raise ValueError("game publication requires an explicit IPv4 address")
    if host == os.environ["ARENA_GAME_IP"]:
        raise ValueError("management and game addresses must differ")
    uvicorn.run("arena.app.main:app", host=host, port=int(os.environ.get("ARENA_MANAGEMENT_PORT", "8443")),
        ssl_certfile=os.environ["ARENA_TLS_CERT"], ssl_keyfile=os.environ["ARENA_TLS_KEY"],
        workers=1, access_log=False)


if __name__ == "__main__":
    main()

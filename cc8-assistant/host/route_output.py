"""Freeze host output on its original activation before it can wait for a writer."""

from protocol import ROUTE_BOUND, SPEAKER, Packet

RouteOwner = tuple[object | None, str | None]


def same_route(owner: RouteOwner, connection: object | None, token: str | None) -> bool:
    return owner[0] is connection and owner[1] == token


def screen_route_current(owner: RouteOwner, connection: object | None, token: str | None) -> bool:
    wire_token: object = getattr(connection, 'route_token', token)
    return (
        connection is not None
        and same_route(owner, connection, token)
        and wire_token == token
        and getattr(connection, 'application_ready', True) is True
    )


def bind_host_packet(packet: Packet, owner: RouteOwner, generation: int | None = None) -> Packet:
    connection, token = owner
    if packet.kind == SPEAKER and generation is not None and packet.generation != generation:
        return packet
    # An already bound packet must never be retagged with the successor token.
    if (
        packet.kind != ROUTE_BOUND
        and token is not None
        and getattr(connection, 'route_duplex', False) is True
    ):
        return packet.bind_route(token)
    return packet


def host_packet_current(packet: Packet, owner: RouteOwner, generation: int) -> bool:
    connection, token = owner
    if packet.kind == ROUTE_BOUND:
        if token is None or getattr(connection, 'route_duplex', False) is not True:
            return False
        decoded = packet.unbind_route(token)
        if decoded is None:
            return False
        packet = decoded
    return packet.kind != SPEAKER or packet.generation == generation

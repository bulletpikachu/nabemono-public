from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RadioStation:
    id: str
    name: str
    description: str
    stream_url: str

    def public_payload(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
        }


RADIO_STATIONS = (
    RadioStation(
        id="KDFC",
        name="KDFC",
        description="Classical California",
        stream_url="https://16603.live.streamtheworld.com:443/KUSCAAC96_SC",
    ),
)

RADIO_STATIONS_BY_ID = {station.id: station for station in RADIO_STATIONS}


def get_radio_station(station_id: str) -> RadioStation | None:
    return RADIO_STATIONS_BY_ID.get(station_id)

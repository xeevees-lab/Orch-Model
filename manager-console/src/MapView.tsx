import { useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import { Protocol } from "pmtiles";
import "maplibre-gl/dist/maplibre-gl.css";
import { api, type Band, type PressureRow } from "./api";

const TILES = "pmtiles:///mumbai.pmtiles";

export const INK = {
  base: "#f2f0ea",
  land: "#eae7df",
  water: "#cfe0e9",
  road: "#dcd7cd",
  roadMajor: "#c6bfb2",
  building: "#e1ddd3",
};

export const BAND_COLOR: Record<Band, string> = {
  ok: "#0f8b7e",
  warning: "#c8890f",
  critical: "#cf4429",
};

export const SUPPLY_RAMP: [number, string][] = [
  [0, "#f7edd9"],
  [500, "#efd8a4"],
  [1500, "#e2bb69"],
  [3000, "#cb9a32"],
  [6000, "#a5760f"],
];

// Registered once at module scope. Doing this in an effect means
// StrictMode's mount-unmount-remount can tear the protocol down while
// a map is still resolving its style, and the style then fails
// silently - blank canvas, no error.
const pmtilesProtocol = new Protocol();
maplibregl.addProtocol("pmtiles", pmtilesProtocol.tile);

function baseStyle(): maplibregl.StyleSpecification {
  return {
    version: 8,
    glyphs: "https://fonts.openmaptiles.org/{fontstack}/{range}.pbf",
    sources: {
      basemap: { type: "vector", url: TILES, attribution: "© OpenStreetMap" },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": INK.base } },
      { id: "earth", type: "fill", source: "basemap", "source-layer": "earth",
        paint: { "fill-color": INK.land } },
      { id: "water", type: "fill", source: "basemap", "source-layer": "water",
        paint: { "fill-color": INK.water } },
      { id: "buildings", type: "fill", source: "basemap",
        "source-layer": "buildings", minzoom: 14,
        paint: { "fill-color": INK.building } },
      { id: "roads-minor", type: "line", source: "basemap",
        "source-layer": "roads", filter: ["!=", ["get", "kind"], "highway"],
        paint: { "line-color": INK.road, "line-width": 0.6 } },
      { id: "roads-major", type: "line", source: "basemap",
        "source-layer": "roads", filter: ["==", ["get", "kind"], "highway"],
        paint: { "line-color": INK.roadMajor, "line-width": 1.6 } },
    ],
  };
}

export default function MapView({
  board, selected, onSelect,
}: {
  board: PressureRow[];
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const venuesGeo = useRef<GeoJSON.FeatureCollection | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current || map.current) return;
    const m = new maplibregl.Map({
      container: container.current,
      style: baseStyle(),
      center: [72.85, 19.03],
      zoom: 10.6,
      attributionControl: { compact: true },
    });
    map.current = m;
    m.addControl(new maplibregl.NavigationControl({}), "bottom-right");

    m.on("load", async () => {
      try {
        const [z, v] = await Promise.all([api.zones(), api.venues()]);
        venuesGeo.current = v;

        m.addSource("zones", { type: "geojson", data: z });
        m.addLayer({
          id: "zones-fill", type: "fill", source: "zones",
          paint: {
            "fill-color": ["interpolate", ["linear"], ["get", "rooms_total"],
              ...SUPPLY_RAMP.flat()],
            "fill-opacity": 0.55,
          },
        });
        m.addLayer({
          id: "zones-line", type: "line", source: "zones",
          paint: { "line-color": "#c9c3b8", "line-width": 0.6 },
        });

        m.addSource("venues", { type: "geojson", data: v });
        m.addLayer({
          id: "venue-halo", type: "circle", source: "venues",
          paint: {
            "circle-radius": ["interpolate", ["linear"],
              ["coalesce", ["get", "queue"], 0], 0, 6, 60000, 44],
            "circle-color": ["coalesce", ["get", "band_color"], "#8899aa"],
            "circle-opacity": 0.2,
          },
        });
        m.addLayer({
          id: "venue-dot", type: "circle", source: "venues",
          paint: {
            "circle-radius": 5,
            "circle-color": ["coalesce", ["get", "band_color"], "#8899aa"],
            "circle-stroke-width": 1.5,
            "circle-stroke-color": "#ffffff",
          },
        });

        m.on("click", "venue-dot", (e) => {
          const id = e.features?.[0]?.properties?.node_id;
          if (id) onSelect(Number(id));
        });
        m.on("mouseenter", "venue-dot", () => {
          m.getCanvas().style.cursor = "pointer";
        });
        m.on("mouseleave", "venue-dot", () => {
          m.getCanvas().style.cursor = "";
        });
        m.resize();
        setReady(true);
      } catch (err) {
        setError("Cannot reach the API on :8000.");
        console.error(err);
      }
    });

    // The map is created before the flex layout settles, so the canvas
    // can size to zero and stay there. Re-measure whenever the
    // container's box changes.
    const ro = new ResizeObserver(() => m.resize());
    ro.observe(container.current!);

    return () => { ro.disconnect(); m.remove(); map.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const m = map.current;
    if (!m || !ready || !venuesGeo.current) return;
    const byId = new Map(board.map((r) => [r.node_id, r]));
    const fc: GeoJSON.FeatureCollection = {
      type: "FeatureCollection",
      features: venuesGeo.current.features.map((f) => {
        const row = byId.get(Number(f.properties?.node_id));
        return {
          ...f,
          properties: {
            ...f.properties,
            queue: row?.queue ?? 0,
            band_color: BAND_COLOR[(row?.band ?? "ok") as Band],
          },
        };
      }),
    };
    (m.getSource("venues") as maplibregl.GeoJSONSource | undefined)?.setData(fc);
  }, [board, ready]);

  useEffect(() => {
    const m = map.current;
    if (!m || selected === null) return;
    const f = venuesGeo.current?.features.find(
      (x) => Number(x.properties?.node_id) === selected
    );
    if (f && f.geometry.type === "Point") {
      m.flyTo({ center: f.geometry.coordinates as [number, number], zoom: 13 });
    }
  }, [selected]);

  return (
    <div className="map-wrap">
      <div ref={container} className="map" />
      {error && <div className="alert">{error}</div>}
      <aside className="legend">
        <div className="legend-title">Venue pressure</div>
        {(["ok", "warning", "critical"] as Band[]).map((b) => (
          <div className="key" key={b}>
            <i style={{ background: BAND_COLOR[b] }} />
            {b === "ok" ? "Within limits"
              : b === "warning" ? "Building" : "Over the line"}
          </div>
        ))}
        <div className="legend-title spaced">Zone room supply</div>
        <div className="ramp">
          {SUPPLY_RAMP.map(([v, c]) => (
            <div className="swatch" key={v}>
              <span style={{ background: c }} />
              <em>{v === 6000 ? "6k+" : v >= 1000 ? `${v / 1000}k` : v}</em>
            </div>
          ))}
        </div>
      </aside>
    </div>
  );
}

import type { Delta, Seat, SeatStatus } from "./api";

export const AVAILABLE = 0;
export const HELD = 1;
export const SOLD = 2;
const statusCode: Record<SeatStatus, number> = { available: AVAILABLE, held: HELD, sold: SOLD };
export const statusName = ["available", "held", "sold"] as const;
export type DeltaResult = "applied" | "duplicate" | "gap" | "layout-mismatch";

export class SeatModel {
  readonly states: Uint8Array;
  readonly versions: Float64Array;
  private readonly positionByIndex = new Map<number, number>();

  constructor(readonly seats: Seat[], public applied: number, previous?: SeatModel) {
    this.states = new Uint8Array(seats.length);
    this.versions = new Float64Array(seats.length);
    const previousById = previous ? new Map(previous.seats.map((seat, index) => [seat.id, index])) : undefined;
    seats.forEach((seat, position) => {
      this.positionByIndex.set(seat.idx, position);
      this.states[position] = statusCode[seat.status];
      this.versions[position] = seat.version;
      // A resync can hit a recently cached snapshot. Carry over newer
      // versions by seat ID because an import can renumber stream indices.
      const oldPosition = previousById?.get(seat.id);
      if (previous && oldPosition !== undefined && previous.versions[oldPosition]! > seat.version) {
        this.versions[position] = previous.versions[oldPosition]!;
        this.states[position] = previous.states[oldPosition]!;
      }
    });
  }

  apply(delta: Delta, changed: (position: number) => void): DeltaResult {
    if (!Number.isSafeInteger(delta.from) || !Number.isSafeInteger(delta.to)
      || delta.from > delta.to || delta.from < 1) return "layout-mismatch";
    if (delta.to <= this.applied) return "duplicate";
    if (delta.from > this.applied + 1) return "gap";
    // Validate the whole frame before changing the cursor or any seat. An unknown
    // index indicates that the frame and snapshot may describe different layouts.
    if (delta.changes.some(([idx, status, version]) => !this.positionByIndex.has(idx)
      || !Object.hasOwn(statusCode, status) || !Number.isSafeInteger(version))) return "layout-mismatch";
    for (const [idx, status, version] of delta.changes) {
      const position = this.positionByIndex.get(idx)!;
      if (version <= this.versions[position]!) continue;
      this.versions[position] = version;
      const next = statusCode[status];
      if (this.states[position] !== next) {
        this.states[position] = next;
        changed(position);
      }
    }
    this.applied = delta.to;
    return "applied";
  }
}

export const CELL = 19;
const SIZE = 14;
const PAD = 52;
const TOP = 38;
const GAP = 18;
const MAX_COLUMNS = 56;
const compare = new Intl.Collator(undefined, { numeric: true });

interface GridRow { label: string; cells: number[] }
interface GridZone { code: string; y: number; rows: GridRow[] }

export class SeatGrid {
  readonly zones: GridZone[] = [];
  readonly x: Int32Array;
  readonly y: Int32Array;
  readonly gridRow: Int32Array;
  readonly width: number;
  readonly height: number;
  readonly rows: GridRow[] = [];

  constructor(seats: Seat[]) {
    this.x = new Int32Array(seats.length);
    this.y = new Int32Array(seats.length);
    this.gridRow = new Int32Array(seats.length);
    const grouped = new Map<string, Map<string, number[]>>();
    seats.forEach((seat, index) => {
      let rows = grouped.get(seat.zone);
      if (!rows) grouped.set(seat.zone, rows = new Map<string, number[]>());
      let cells = rows.get(seat.row);
      if (!cells) rows.set(seat.row, cells = []);
      cells.push(index);
    });
    let height = GAP;
    let maxColumns = 0;
    for (const [code, rows] of grouped) {
      const zone: GridZone = { code, y: height, rows: [] };
      for (const [row, cells] of [...rows].sort(([a], [b]) => compare.compare(a, b))) {
        cells.sort((a, b) => compare.compare(String(seats[a]!.number), String(seats[b]!.number)));
        for (let start = 0; start < cells.length; start += MAX_COLUMNS) {
          const slice = cells.slice(start, start + MAX_COLUMNS);
          const y = height + TOP + zone.rows.length * CELL;
          slice.forEach((position, column) => {
            this.x[position] = PAD + column * CELL;
            this.y[position] = y;
            this.gridRow[position] = this.rows.length;
          });
          const entry = { label: start === 0 ? row : "", cells: slice };
          zone.rows.push(entry);
          this.rows.push(entry);
          maxColumns = Math.max(maxColumns, slice.length);
        }
      }
      this.zones.push(zone);
      height += TOP + zone.rows.length * CELL + GAP;
    }
    this.height = height;
    this.width = PAD + maxColumns * CELL + GAP;
  }

  hit(x: number, y: number): number | undefined {
    if (x < PAD || (x - PAD) % CELL >= SIZE) return undefined;
    // Zone blocks are laid out vertically. Binary search keeps pointer work
    // bounded even when an import defines a large number of zones.
    let low = 0;
    let high = this.zones.length - 1;
    while (low <= high) {
      const middle = (low + high) >>> 1;
      const zone = this.zones[middle]!;
      const offset = y - zone.y - TOP;
      if (y < zone.y) high = middle - 1;
      else if (offset >= zone.rows.length * CELL + GAP) low = middle + 1;
      else if (offset >= 0 && offset % CELL < SIZE) {
        return zone.rows[Math.floor(offset / CELL)]?.cells[Math.floor((x - PAD) / CELL)];
      } else return undefined;
    }
    return undefined;
  }

  neighbor(position: number, dx: number, dy: number): number {
    const row = this.gridRow[position]!;
    const next = this.rows[row + dy]?.cells;
    if (!next) return position;
    const column = (this.x[position]! - PAD) / CELL;
    return next[Math.max(0, Math.min(column + dx, next.length - 1))] ?? position;
  }

  visible(left: number, top: number, width: number, height: number,
    drawZone: (zone: GridZone) => void, drawRow: (label: string, y: number) => void,
    drawSeat: (position: number) => void,
  ): void {
    const bottom = top + height;
    for (const zone of this.zones) {
      const firstY = zone.y + TOP;
      if (zone.y > bottom) break;
      if (firstY + zone.rows.length * CELL < top) continue;
      drawZone(zone);
      const first = Math.max(0, Math.floor((top - firstY) / CELL));
      const end = Math.min(zone.rows.length, Math.ceil((bottom - firstY) / CELL));
      const firstCol = Math.max(0, Math.floor((left - PAD) / CELL));
      const endCol = Math.ceil((left + width - PAD) / CELL);
      for (let index = first; index < end; index++) {
        const row = zone.rows[index]!;
        if (left < PAD) drawRow(row.label, firstY + index * CELL);
        const last = Math.min(endCol, row.cells.length);
        for (let column = firstCol; column < last; column++) drawSeat(row.cells[column]!);
      }
    }
  }
}

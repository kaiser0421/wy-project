import type { SeatModel } from "./seat-model";
import { AVAILABLE, CELL, HELD, SeatGrid, statusName } from "./seat-model";
import { onThemeChange } from "./theme";
import { el } from "./view";

const size = 14;

function seatColors() {
  const styles = getComputedStyle(document.documentElement);
  const color = (token: string) => styles.getPropertyValue(token).trim();
  return {
    background: color("--seat-background"),
    zone: color("--seat-zone"),
    row: color("--muted"),
    states: [color("--seat-available"), color("--seat-held"), color("--seat-sold")],
    selected: color("--seat-selected"),
    symbol: color("--seat-symbol"),
    outline: color("--seat-outline"),
  };
}

export class SeatCanvas {
  readonly viewport: HTMLDivElement;
  readonly announcement = el("p", { class: "seat-readout", "aria-live": "polite" });
  private readonly canvas: HTMLCanvasElement;
  private readonly context: CanvasRenderingContext2D;
  private readonly grid: SeatGrid;
  private readonly queued: Uint8Array;
  private readonly dirty: number[] = [];
  private readonly selected = new Set<number>();
  private frame = 0;
  private full = true;
  private active: number;
  private highlighted = false;
  private readonly resize: ResizeObserver;
  private readonly theme = new AbortController();
  private colors = seatColors();

  constructor(private readonly model: SeatModel, private readonly toggle: (position: number) => void,
    selectable: boolean) {
    this.grid = new SeatGrid(model.seats);
    this.queued = new Uint8Array(model.seats.length);
    this.active = this.grid.rows[0]?.cells[0] ?? -1;
    const instructions = el("p", { class: "sr-only", id: "seat-instructions" },
      selectable ? "Arrow keys move between seats. Press Space or Enter to select a seat."
        : "Arrow keys move between seats and announce their status.");
    this.canvas = el("canvas", { tabindex: 0, "aria-label": "Interactive seat map",
      "aria-describedby": instructions.id });
    const context = this.canvas.getContext("2d", { alpha: false });
    if (!context) throw new Error("Canvas is unavailable");
    this.context = context;
    const surface = el("div", { class: "map-surface" }, this.canvas);
    surface.style.width = `${this.grid.width}px`;
    surface.style.height = `${this.grid.height}px`;
    this.viewport = el("div", { class: "map-viewport" }, instructions, surface);
    this.viewport.addEventListener("scroll", this.redraw, { passive: true });
    this.canvas.onfocus = () => {
      this.highlighted = true;
      this.changeFocus(this.active);
    };
    this.canvas.onblur = () => {
      this.highlighted = false;
      this.mark(this.active);
    };
    this.canvas.onclick = event => {
      const rect = this.canvas.getBoundingClientRect();
      const position = this.grid.hit(event.clientX - rect.left + this.viewport.scrollLeft,
        event.clientY - rect.top + this.viewport.scrollTop);
      if (position === undefined) return;
      this.changeFocus(position);
      if (selectable) this.toggle(position);
    };
    this.canvas.onkeydown = event => {
      const keys: Record<string, [number, number]> = {
        ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0],
      };
      const direction = keys[event.key];
      if (this.active < 0 || (!direction && event.key !== " " && event.key !== "Enter")) return;
      event.preventDefault();
      if (direction) this.changeFocus(this.grid.neighbor(this.active, ...direction));
      else if (selectable) this.toggle(this.active);
    };
    this.resize = new ResizeObserver(this.redraw);
    this.resize.observe(this.viewport);
    onThemeChange(() => {
      this.colors = seatColors();
      this.redraw();
    }, this.theme.signal);
    this.schedule();
  }

  mark(position: number): void {
    if (position < 0 || this.queued[position]) return;
    this.queued[position] = 1;
    this.dirty.push(position);
    this.schedule();
  }

  select(position: number, selected: boolean): void {
    if (selected) this.selected.add(position);
    else this.selected.delete(position);
    this.mark(position);
    if (position === this.active) this.describe();
  }

  describe(): void {
    const seat = this.model.seats[this.active];
    if (!seat) return;
    const state = this.selected.has(this.active) ? "selected" : statusName[this.model.states[this.active]!]!;
    const description = `Zone ${seat.zone}, row ${seat.row}, seat ${seat.number} · ${state}`;
    if (this.announcement.textContent !== description) this.announcement.textContent = description;
  }

  destroy(): void {
    this.theme.abort();
    this.resize.disconnect();
    this.viewport.removeEventListener("scroll", this.redraw);
    cancelAnimationFrame(this.frame);
  }

  private changeFocus(position: number): void {
    if (position < 0) return;
    this.mark(this.active);
    this.active = position;
    this.mark(position);
    this.describe();
    const x = this.grid.x[position]!;
    const y = this.grid.y[position]!;
    const { clientWidth, clientHeight, scrollLeft, scrollTop } = this.viewport;
    if (x < scrollLeft) this.viewport.scrollLeft = x - CELL;
    if (x + CELL > scrollLeft + clientWidth) this.viewport.scrollLeft = x + CELL * 2 - clientWidth;
    if (y < scrollTop) this.viewport.scrollTop = y - CELL;
    if (y + CELL > scrollTop + clientHeight) this.viewport.scrollTop = y + CELL * 2 - clientHeight;
  }

  private redraw = () => {
    this.full = true;
    this.schedule();
  };

  private schedule(): void {
    if (!this.frame) this.frame = requestAnimationFrame(this.draw);
  }

  private draw = () => {
    this.frame = 0;
    const { canvas, context, viewport, grid, colors } = this;
    const width = Math.min(viewport.clientWidth, grid.width);
    const height = Math.min(viewport.clientHeight, grid.height);
    if (!width || !height) return;
    const left = viewport.scrollLeft;
    const top = viewport.scrollTop;
    const dpr = window.devicePixelRatio || 1;
    if (this.full) {
      canvas.width = Math.ceil(width * dpr);
      canvas.height = Math.ceil(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
    }
    context.setTransform(dpr, 0, 0, dpr, -left * dpr, -top * dpr);
    if (this.full) {
      context.fillStyle = colors.background;
      context.fillRect(left, top, width, height);
      grid.visible(left, top, width, height, zone => {
        context.fillStyle = colors.zone;
        context.font = "bold 13px system-ui";
        context.fillText(`ZONE ${zone.code}`, 16, zone.y + 20);
      }, (label, y) => {
        context.fillStyle = colors.row;
        context.font = "11px system-ui";
        context.textAlign = "right";
        context.fillText(label, 42, y + 11, 34);
        context.textAlign = "start";
      }, this.drawSeat);
      this.full = false;
    } else {
      for (const position of this.dirty) {
        const x = grid.x[position]!;
        const y = grid.y[position]!;
        if (x + CELL < left || y + CELL < top || x > left + width || y > top + height) continue;
        context.fillStyle = colors.background;
        context.fillRect(x - 2, y - 2, CELL, CELL);
        this.drawSeat(position);
      }
    }
    for (const position of this.dirty) this.queued[position] = 0;
    this.dirty.length = 0;
  };

  private drawSeat = (position: number) => {
    const { context: ctx, grid, colors } = this;
    const x = grid.x[position]!;
    const y = grid.y[position]!;
    const state = this.model.states[position]!;
    const chosen = this.selected.has(position);
    ctx.fillStyle = chosen ? colors.selected : colors.states[state]!;
    ctx.fillRect(x, y, size, size);
    if (chosen) {
      ctx.strokeStyle = colors.symbol;
      ctx.beginPath();
      ctx.moveTo(x + 3, y + 7);
      ctx.lineTo(x + 6, y + 10);
      ctx.lineTo(x + 11, y + 4);
      ctx.stroke();
    } else if (state !== AVAILABLE) {
      ctx.strokeStyle = colors.symbol;
      ctx.beginPath();
      ctx.moveTo(x + 3, y + 11);
      ctx.lineTo(x + 11, y + 3);
      if (state !== HELD) {
        ctx.moveTo(x + 3, y + 3);
        ctx.lineTo(x + 11, y + 11);
      }
      ctx.stroke();
    }
    if (this.highlighted && position === this.active) {
      ctx.strokeStyle = colors.outline;
      ctx.strokeRect(x - 1, y - 1, size + 2, size + 2);
    }
  };
}

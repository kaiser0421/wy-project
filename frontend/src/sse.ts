export interface Frame {
  event: string;
  data: string;
  id?: string;
}

/** A transport chunk can split a line, a CRLF pair, or a UTF-8 character. */
export class EventParser {
  private pending = "";
  private data: string[] = [];
  private event = "";
  private id?: string;
  private skipLF = false;

  push(chunk: string): Frame[] {
    const frames: Frame[] = [];
    let start = 0;
    for (let i = 0; i < chunk.length; i++) {
      const character = chunk[i];
      if (this.skipLF) {
        this.skipLF = false;
        if (character === "\n") {
          start = i + 1;
          continue;
        }
      }
      if (character !== "\r" && character !== "\n") continue;
      this.pending += chunk.slice(start, i);
      const frame = this.line(this.pending);
      if (frame) frames.push(frame);
      this.pending = "";
      this.skipLF = character === "\r";
      start = i + 1;
    }
    this.pending += chunk.slice(start);
    return frames;
  }

  private line(line: string): Frame | undefined {
    if (!line) {
      const frame = this.data.length
        ? { event: this.event || "message", id: this.id, data: this.data.join("\n") }
        : undefined;
      this.event = "";
      this.data = [];
      this.id = undefined;
      return frame;
    }
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") this.event = value;
    if (field === "data") this.data.push(value);
    if (field === "id" && !value.includes("\0")) this.id = value;
  }
}

export async function* readFrames(body: ReadableStream<Uint8Array>): AsyncGenerator<Frame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new EventParser();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        yield* parser.push(decoder.decode());
        return;
      }
      yield* parser.push(decoder.decode(value, { stream: true }));
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

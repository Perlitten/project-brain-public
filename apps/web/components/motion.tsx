import { Fragment, type CSSProperties } from "react";

// Numbers roll into place like an odometer instead of ticking up. Pure CSS:
// each digit column sits at its final offset, and the first paint animates it
// from zero. Later value changes ride a transform transition. Columns are keyed
// from the right so "94" -> "100" keeps the ones and tens drums in place.
// Two turns of digits: the drum starts at the top and lands on the second
// copy, so even a zero spins a full turn before it settles.
const DRUM = "01234567890123456789".split("");

export function Odometer({ value, className = "" }: { value: string; className?: string }) {
  const chars = [...value];
  return (
    <span className={`odo ${className}`} aria-label={value} role="img">
      {chars.map((ch, i) => {
        const key = chars.length - i;
        if (!/\d/.test(ch)) {
          return (
            <span className="odo__static" key={`s${key}`} aria-hidden="true">
              {ch}
            </span>
          );
        }
        return (
          <span className="odo__col" key={`d${key}`} aria-hidden="true">
            <span
              className="odo__drum"
              style={{ "--n": Number(ch), "--i": i } as CSSProperties}
            >
              {DRUM.map((d, k) => (
                <span key={k}>{d}</span>
              ))}
            </span>
          </span>
        );
      })}
    </span>
  );
}

// A headline that rises word by word out of a mask.
export function Rise({ text, delay = 0 }: { text: string; delay?: number }) {
  const words = text.split(" ");
  return (
    <span className="rise" aria-label={text} role="text">
      {words.map((w, i) => (
        <Fragment key={i}>
          <span className="rise__mask" aria-hidden="true">
            <span className="rise__word" style={{ "--w": i, "--rd": `${delay}ms` } as CSSProperties}>
              {w}
            </span>
          </span>
          {i < words.length - 1 ? " " : ""}
        </Fragment>
      ))}
    </span>
  );
}

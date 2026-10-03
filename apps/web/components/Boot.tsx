import type { CSSProperties } from "react";
import { BrandMark } from "./Icon";

// First visit of a browser session opens with a short start-up sequence: the
// mark writes itself line by line, a few log lines type out real facts, a segmented meter fills,
// and the overlay wipes away upwards while the screen enters beneath it.
// Pure CSS; BOOT_SCRIPT (inline, before paint) decides whether it shows at all.

const BOOT_MS = 1500;
const BOOT_CAP_MS = 4000;

// Runs before first paint. Marks the document as booting only once per session
// (and never with reduced motion), hides <main> until the wipe so every screen
// animation starts on cue, and lets any click or key skip ahead. The sequence
// is timed from the moment the document has arrived, so a slow response is
// covered rather than cut short; a hard cap keeps a stalled stream from
// holding the screen.
export const BOOT_SCRIPT = `(function(){try{var d=document.documentElement;
if(sessionStorage.getItem("pb-boot")||matchMedia("(prefers-reduced-motion: reduce)").matches)return;
sessionStorage.setItem("pb-boot","1");d.setAttribute("data-boot","");
var done=false;function end(skip){if(done)return;done=true;d.setAttribute("data-boot-out",skip?"skip":"");
d.removeAttribute("data-boot");setTimeout(function(){d.removeAttribute("data-boot-out")},900)}
function arm(){setTimeout(function(){end(false)},${BOOT_MS})}
if(document.readyState==="loading")addEventListener("DOMContentLoaded",arm,{once:true});else arm();
setTimeout(function(){end(false)},${BOOT_CAP_MS});
addEventListener("pointerdown",function(){end(true)},{once:true});addEventListener("keydown",function(){end(true)},{once:true});
}catch(e){}})();`;

export function Boot({ lines }: { lines: string[] }) {
  return (
    <div className="boot" aria-hidden="true">
      <div className="boot__core">
        <BrandMark size={88} className="mark--intro" />
        <p className="boot__name">Project Brain</p>
        <ol className="boot__log">
          {lines.map((l, i) => (
            <li key={i} style={{ "--l": i, "--len": l.length } as CSSProperties}>
              <span>{l}</span>
            </li>
          ))}
        </ol>
        <div className="boot__meter">
          {Array.from({ length: 20 }, (_, i) => (
            <span key={i} style={{ "--i": i } as CSSProperties} />
          ))}
        </div>
      </div>
    </div>
  );
}

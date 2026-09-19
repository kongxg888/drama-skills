import { continueRender, delayRender } from "remotion";

/**
 * Remotion captures a frame as soon as React has painted, so a font still being
 * fetched is simply absent from it. Nothing downstream reports that: the film
 * measures correct and reads wrong.
 */
export const waitForFonts = (): void => {
  // The subtitle route deliberately defaults to the browser's platform
  // `sans-serif` family. On some macOS/Chromium combinations
  // `document.fonts.ready` never settles for platform fallback fonts, which
  // would make Remotion time out before the first frame. There is no
  // downloadable @font-face in this composition, so the browser can paint
  // the requested system family synchronously.
  if (!document.fonts || document.fonts.status !== "loading") return;
  const handle = delayRender("等待字体就绪");
  const release = () => continueRender(handle);
  document.fonts.ready.then(release, release);
  // A platform fallback may keep the FontFaceSet in `loading` forever even
  // though the browser can already render the frame. Do not hold the whole
  // film hostage to that bookkeeping state.
  setTimeout(release, 2000);
};

/**
 * `document.fonts.ready` promises that loading finished, not that the requested
 * family exists — a missing face raises nothing, the browser substitutes, and
 * the film ships in the wrong typeface. Measuring is the only reliable test:
 * identical widths against a family that cannot exist mean nothing resolved.
 */
let familyChecked = "";

export const assertFamilyResolves = (fontFamily: string, sample: string): void => {
  // Runs on every frame, so measure once per family.
  if (!sample || familyChecked === fontFamily) return;
  // Generic families intentionally resolve through the browser's platform CJK
  // fallback. Comparing them with a deliberately missing family would report
  // that valid fallback as absent, even though the browser can render it.
  if (fontFamily.trim() === "sans-serif") {
    familyChecked = fontFamily;
    return;
  }
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  if (!context) return;

  const measure = (family: string): number => {
    context.font = `700 64px ${family}`;
    return context.measureText(sample).width;
  };
  // A name no font can carry, so it always lands on the browser's last resort.
  const fallbackOnly = measure('"__no_such_family__"');
  const requested = measure(`${fontFamily}, "__no_such_family__"`);
  if (requested === fallbackOnly) {
    throw new Error(
      `字幕字体 ${fontFamily} 在渲染环境里一个都没装上，` +
        "画面会落到浏览器的兜底字体。装一款其中的字体，" +
        "或给 render 传一个本机确实有的字体族。",
    );
  }
  familyChecked = fontFamily;
};

"use client";

import { useEffect, useRef, type ReactNode } from "react";

export function HeroParallax({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLElement>(null);

  useEffect(() => {
    const hero = ref.current;
    if (!hero) return;
    const preference = window.matchMedia(
      "(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)",
    );
    let current = { x: 0, y: 0 },
      target = { x: 0, y: 0 },
      frame = 0,
      lastTime = 0;

    function animate(time: number) {
      frame = 0;
      const blend = lastTime
        ? 1 - Math.exp(-Math.min(time - lastTime, 64) / 90)
        : 0.18;
      lastTime = time;
      current.x += (target.x - current.x) * blend;
      current.y += (target.y - current.y) * blend;
      const settled =
        Math.abs(target.x - current.x) < 0.001 &&
        Math.abs(target.y - current.y) < 0.001;
      if (settled) current = { ...target };
      hero!.style.setProperty("--hero-parallax-x", current.x.toFixed(4));
      hero!.style.setProperty("--hero-parallax-y", current.y.toFixed(4));
      if (!settled) frame = requestAnimationFrame(animate);
      else lastTime = 0;
    }

    function aim(x: number, y: number) {
      target = { x, y };
      if (!frame) frame = requestAnimationFrame(animate);
    }

    function move(event: PointerEvent) {
      if (!preference.matches || event.pointerType !== "mouse") return;
      const bounds = hero!.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      const clamp = (value: number) => Math.max(-1, Math.min(1, value));
      aim(
        clamp(((event.clientX - bounds.left) / bounds.width) * 2 - 1),
        clamp(((event.clientY - bounds.top) / bounds.height) * 2 - 1),
      );
    }

    function reset() {
      if (current.x || current.y || target.x || target.y) aim(0, 0);
    }

    function clear() {
      cancelAnimationFrame(frame);
      frame = lastTime = 0;
      current = target = { x: 0, y: 0 };
      hero!.style.removeProperty("--hero-parallax-x");
      hero!.style.removeProperty("--hero-parallax-y");
    }

    function visibilityChanged() {
      if (document.hidden) clear();
    }

    hero.addEventListener("pointermove", move, { passive: true });
    hero.addEventListener("pointerleave", reset);
    hero.addEventListener("pointercancel", reset);
    window.addEventListener("blur", reset);
    document.addEventListener("visibilitychange", visibilityChanged);
    preference.addEventListener("change", clear);
    return () => {
      hero.removeEventListener("pointermove", move);
      hero.removeEventListener("pointerleave", reset);
      hero.removeEventListener("pointercancel", reset);
      window.removeEventListener("blur", reset);
      document.removeEventListener("visibilitychange", visibilityChanged);
      preference.removeEventListener("change", clear);
      clear();
    };
  }, []);

  return (
    <section ref={ref} className="hero panel">
      {children}
    </section>
  );
}

"use client";

import { useEffect, useRef } from "react";

// Ambient animated dotted wave (Three.js). Three is dynamically imported so it never
// enters the initial bundle or SSR.
export function DottedSurface({ className }: { className?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let mounted = true;
    let cleanup = () => {};

    import("three").then((THREE) => {
      if (!mounted || !el) return;
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      const SEP = 22;
      const AMOUNTX = 104;
      const AMOUNTY = 38;

      const width = () => el.clientWidth || 1;
      const height = () => el.clientHeight || 1;

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(60, width() / height(), 1, 10000);
      camera.position.set(0, 185, 360);
      camera.lookAt(0, -42, 0);

      const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.setSize(width(), height());
      el.appendChild(renderer.domElement);

      const n = AMOUNTX * AMOUNTY;
      const positions = new Float32Array(n * 3);
      let p = 0;
      for (let ix = 0; ix < AMOUNTX; ix++) {
        for (let iy = 0; iy < AMOUNTY; iy++) {
          positions[p] = ix * SEP - (AMOUNTX * SEP) / 2;
          positions[p + 1] = 0;
          positions[p + 2] = iy * SEP - (AMOUNTY * SEP) / 2;
          p += 3;
        }
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const material = new THREE.PointsMaterial({
        color: 0xd6d7d9,
        size: 2.55,
        sizeAttenuation: true,
        transparent: true,
        opacity: 0.56,
      });
      const dots = new THREE.Points(geometry, material);
      scene.add(dots);

      let raf = 0;
      let count = 0;
      const render = () => {
        const arr = geometry.attributes.position.array as Float32Array;
        let k = 0;
        for (let ix = 0; ix < AMOUNTX; ix++) {
          for (let iy = 0; iy < AMOUNTY; iy++) {
            arr[k + 1] =
              Math.sin((ix + count) * 0.26) * 16 + Math.sin((iy + count) * 0.42) * 14;
            k += 3;
          }
        }
        geometry.attributes.position.needsUpdate = true;
        renderer.render(scene, camera);
        count += 0.055;
      };
      const loop = () => {
        render();
        raf = requestAnimationFrame(loop);
      };
      if (reduced) render();
      else loop();

      const onResize = () => {
        camera.aspect = width() / height();
        camera.updateProjectionMatrix();
        renderer.setSize(width(), height());
      };
      window.addEventListener("resize", onResize);

      cleanup = () => {
        window.removeEventListener("resize", onResize);
        cancelAnimationFrame(raf);
        geometry.dispose();
        material.dispose();
        renderer.dispose();
        renderer.domElement.remove();
      };
    });

    return () => {
      mounted = false;
      cleanup();
    };
  }, []);

  return <div ref={ref} className={className} aria-hidden />;
}

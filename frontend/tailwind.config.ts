import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // SOKKAN dark palette (gouvernail: deep navy + brass accent)
        ink: "#0c1018",
        panel: "#141a24",
        panel2: "#1b232f",
        line: "#26303d",
        brass: "#D4A017",
        sea: "#3b82f6",
        mut: "#8aa0b6",
      },
    },
  },
  plugins: [],
} satisfies Config;

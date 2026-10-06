import next from "eslint-config-next";

/**
 * eslint-config-next v16 直接导出 flat config，不需要 FlatCompat 桥接。
 */
const config = [
  ...next,
  {
    // components/ui 从 registry 生成，不手改，lint 忽略（M5 §2）
    ignores: [
      ".next/**",
      "node_modules/**",
      "next-env.d.ts",
      "playwright-report/**",
      "test-results/**",
      "src/components/ui/**",
    ],
  },
];

export default config;

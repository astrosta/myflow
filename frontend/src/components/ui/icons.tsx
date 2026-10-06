import type { ComponentProps } from "react";

/**
 * 内联 SVG 图标。
 *
 * 不引图标库：整个应用只用到两个图标，为它们拉一个包（以及它在
 * optimizePackageImports 里的额外配置）不划算。颜色用 currentColor，
 * 所以完全由外层的文字色决定。
 */

export interface IconProps extends ComponentProps<"svg"> {
  /** 视觉尺寸（px）。默认 20，正好配 h-9 的圆形按钮 */
  size?: number;
}

function Icon({ size = 20, children, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={2}
      viewBox="0 0 24 24"
      width={size}
      {...props}
    >
      {children}
    </svg>
  );
}

/** 发送。向上箭头——和 DeepSeek / 千问网页端的输入框一致 */
export function ArrowUpIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 19V5" />
      <path d="m5 12 7-7 7 7" />
    </Icon>
  );
}

/** 停止。实心方块，比 "■" 字符的基线更可控 */
export function StopIcon({ size = 18, ...props }: IconProps) {
  return (
    <svg aria-hidden="true" fill="currentColor" height={size} viewBox="0 0 24 24" width={size} {...props}>
      <rect height="12" rx="2.5" width="12" x="6" y="6" />
    </svg>
  );
}

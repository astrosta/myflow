import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CreateKnowledgeBaseDialog } from "./create-dialog";

afterEach(cleanup);

describe("CreateKnowledgeBaseDialog", () => {
  it("shows all five types and requires a name", () => {
    const onCreated = vi.fn();
    render(<CreateKnowledgeBaseDialog onClose={() => {}} onCreated={onCreated} />);

    expect(screen.getByRole("dialog", { name: "新建知识库" })).toBeTruthy();
    expect(screen.getByRole("radio", { name: /技术文档/ })).toBeTruthy();
    expect(screen.getByRole("radio", { name: /运维手册/ })).toBeTruthy();
    expect(screen.getByRole("radio", { name: /FAQ 类文档/ })).toBeTruthy();
    expect(screen.getByRole("radio", { name: /安全合规文档/ })).toBeTruthy();
    expect(screen.getByRole("radio", { name: /API 参考文档/ })).toBeTruthy();

    const create = screen.getByRole("button", { name: "创建" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("知识库名称"), { target: { value: "产品文档" } });
    expect(create.disabled).toBe(false);
  });

  it("shows the selected type description", () => {
    render(<CreateKnowledgeBaseDialog onClose={() => {}} onCreated={vi.fn()} />);
    fireEvent.click(screen.getByRole("radio", { name: /FAQ 类文档/ }));
    expect(screen.getByText(/一问一答为一个分片/)).toBeTruthy();
  });
});

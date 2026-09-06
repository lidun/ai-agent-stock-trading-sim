import { useState } from "react";
import { Button, Input, App as AntApp, theme as antTheme } from "antd";
import { SendOutlined } from "@ant-design/icons";

export function Composer({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (text: string) => Promise<boolean>;
}) {
  const { message } = AntApp.useApp();
  const { token } = antTheme.useToken();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  const submit = async () => {
    const body = text.trim();
    if (!body || sending) return;
    setSending(true);
    try {
      const ok = await onSend(body);
      if (ok) setText("");
    } catch (e) {
      message.error((e as Error).message ?? "发送失败");
    } finally {
      setSending(false);
    }
  };

  return (
    <div
      style={{
        borderTop: `1px solid ${token.colorBorderSecondary}`,
        padding: "10px 16px",
        background: token.colorBgContainer,
      }}
    >
      <Input.TextArea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onPressEnter={(e) => {
          // 中文等输入法组合态按下 Enter 仅为选词，不触发发送
          const ne = e.nativeEvent as KeyboardEvent;
          if (!e.shiftKey && !ne.isComposing) {
            e.preventDefault();
            void submit();
          }
        }}
        placeholder="输入消息，Enter 发送 / Shift+Enter 换行"
        autoSize={{ minRows: 1, maxRows: 5 }}
        maxLength={20000}
        disabled={disabled}
        style={{ resize: "none" }}
      />
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
        <Button
          type="primary"
          icon={<SendOutlined />}
          loading={sending}
          disabled={disabled || !text.trim()}
          onClick={() => void submit()}
        >
          发送
        </Button>
      </div>
    </div>
  );
}

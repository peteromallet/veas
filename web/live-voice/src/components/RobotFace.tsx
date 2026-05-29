import type { VoiceState } from "../mic";

type RobotMood =
  | "offline"
  | "connecting"
  | "listening"
  | "hearing"
  | "thinking"
  | "speaking"
  | "paused"
  | "error";

interface Props {
  botName: string;
  status: "connecting" | "open" | "live" | "closed" | "error";
  voice: VoiceState;
  botSpeaking: boolean;
  thinking: boolean;
  paused: boolean;
  reconnecting: boolean;
  size?: "default" | "large";
}

function resolveMood({
  status,
  voice,
  botSpeaking,
  thinking,
  paused,
  reconnecting,
}: Omit<Props, "botName">): RobotMood {
  if (status === "error") return "error";
  if (reconnecting || status === "connecting" || status === "open") {
    return "connecting";
  }
  if (status === "closed") return "offline";
  if (paused) return "paused";
  if (botSpeaking) return "speaking";
  if (voice === "active") return "hearing";
  if (thinking) return "thinking";
  return "listening";
}

const moodCopy: Record<RobotMood, string> = {
  offline: "offline",
  connecting: "connecting",
  listening: "listening",
  hearing: "hearing you",
  thinking: "thinking",
  speaking: "speaking",
  paused: "paused",
  error: "needs attention",
};

export function RobotFace({
  botName,
  status,
  voice,
  botSpeaking,
  thinking,
  paused,
  reconnecting,
  size = "default",
}: Props) {
  const mood = resolveMood({
    status,
    voice,
    botSpeaking,
    thinking,
    paused,
    reconnecting,
  });

  return (
    <div
      className={`robot-face robot-face--${size} robot-face--${mood}`}
      role="img"
      aria-label={`${botName} is ${moodCopy[mood]}`}
    >
      <div className="robot-face__antenna" aria-hidden />
      <div className="robot-face__head">
        <div className="robot-face__screen">
          <div className="robot-face__scanline" aria-hidden />
          <div className="robot-face__eyes" aria-hidden>
            <span className="robot-face__eye robot-face__eye--left" />
            <span className="robot-face__eye robot-face__eye--right" />
          </div>
          <div className="robot-face__mouth" aria-hidden>
            <span />
            <span />
            <span />
            <span />
            <span />
          </div>
        </div>
      </div>
      <div className="robot-face__caption">
        <span className="robot-face__pulse" aria-hidden />
        <span>{moodCopy[mood]}</span>
      </div>
    </div>
  );
}

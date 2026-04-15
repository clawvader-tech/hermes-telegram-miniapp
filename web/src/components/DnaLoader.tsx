export function DnaLoader() {
  return (
    <div className="dna-helix">
      {Array.from({ length: 8 }, (_, i) => {
        const delay = `${i * 0.15}s`;
        return (
          <div className="dna-pair" key={i}>
            <div className="dna-dot dna-dot-l" style={{ animationDelay: delay }} />
            <div className="dna-rung" style={{ animationDelay: delay }} />
            <div className="dna-dot dna-dot-r" style={{ animationDelay: delay }} />
          </div>
        );
      })}
      <style>{`
        .dna-helix {
          display: flex;
          align-items: center;
          height: 32px;
          gap: 5px;
          padding: 4px 8px;
        }
        .dna-pair {
          position: relative;
          width: 6px;
          height: 28px;
        }
        .dna-dot {
          position: absolute;
          width: 6px;
          height: 6px;
          border-radius: 50%;
          left: 0;
        }
        .dna-dot-l {
          background: #ffe6cb;
          animation: dnaL 1.6s ease-in-out infinite;
        }
        .dna-dot-r {
          background: #ffbd38;
          animation: dnaR 1.6s ease-in-out infinite;
        }
        .dna-rung {
          position: absolute;
          width: 6px;
          left: 0;
          top: 13px;
          height: 2px;
          background: #ffe6cb;
          border-radius: 1px;
          animation: dnaRun 1.6s ease-in-out infinite;
        }
        @keyframes dnaL {
          0%, 100% { top: 0px; opacity: 1; }
          50% { top: 22px; opacity: 0.5; }
        }
        @keyframes dnaR {
          0%, 100% { top: 22px; opacity: 0.5; }
          50% { top: 0px; opacity: 1; }
        }
        @keyframes dnaRun {
          0%, 100% { opacity: 0; height: 2px; top: 13px; }
          25%, 75% { opacity: 0.3; height: 18px; top: 5px; }
          50% { opacity: 0; height: 2px; top: 13px; }
        }
      `}</style>
    </div>
  );
}

import { useState } from "react";

export default function CRMPage() {
  const [loading, setLoading] = useState(true);

  return (
    <div className="flex flex-col h-full">
      {loading && (
        <div className="absolute inset-0 flex items-center justify-center bg-background/80 z-10">
          <div className="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full" />
        </div>
      )}
      <iframe
        src="/crm/"
        className="w-full h-full border-0 flex-1"
        onLoad={() => setLoading(false)}
        style={{ minHeight: "70vh" }}
        title="Twenty CRM"
      />
    </div>
  );
}

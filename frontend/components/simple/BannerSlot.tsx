"use client";
// Simple profile — the place of an instance-wide band above the content. 3.6: the study-mode
// band (« your actions are recorded under the pseudonym P2 ») and its dialog « What is
// recorded »; it renders nothing when the feature `study_mode` is off or no study is active.
import StudyBand from "./StudyConsent";

export default function BannerSlot() {
  return <StudyBand />;
}

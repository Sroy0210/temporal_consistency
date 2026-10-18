export default function About({ status }) {
  return (
    <main className="about">
      <h2>How the answers are kept to what was known</h2>
      <p>
        Tools that explain past decisions usually read the whole history, including what happened afterwards.
        This one rebuilds what the decider could see on the day, and answers from that alone.
      </p>

      <h3>The decision</h3>
      <p>
        A decision is a patch being applied to the ext4 tree. The decider is the maintainer who applied it, and
        the moment is when they applied it. The reasoning in the commit message is the patch author's.
      </p>

      <h3>Who knew what, and when</h3>
      <p>
        Every patch has two dates: when it was written, and when it was applied and became public. Authors know
        their own patch from the day they write it; anyone else is credited only from the day it was applied.
        That rule can miss early knowledge, but it never credits anyone with something they could not have seen.
      </p>

      <h3>Four ways to choose evidence</h3>
      <dl className="policies">
        <div><dt>No time filter</dt><dd>Everything in the history, including later fixes. This is what most tools do.</dd></div>
        <div><dt>Cutoff on author date</dt><dd>Patches written before the decision. It looks safe, but a patch written in January and applied in March was still unknown in February.</dd></div>
        <div><dt>Cutoff on applied date</dt><dd>Everything public before the decision: what anyone could have known.</dd></div>
        <div><dt>Information set</dt><dd>Only patches the decider took part in, from the day they could have seen each one: what this person knew. The answers on this site use this unless you compare.</dd></div>
      </dl>

      <h3>The model reads; code decides</h3>
      <ul>
        <li>The time limit is applied before the model sees anything. No search the model can run reaches past it.</li>
        <li>Every claim must quote its evidence word for word. Code checks each quote and removes any that do not match.</li>
        <li>People's names are replaced with placeholders before the model sees them, so it cannot answer from memory of who they are.</li>
        <li>What happened later is added by code, in its own column, and is never shown to the model.</li>
      </ul>

      <h3>Where this goes next</h3>
      <p>
        Git history is the test bed because every event has an exact time and a named author. The same method
        applies to letters, minutes and telegrams, where who knew what, and when, is the question historians ask.
      </p>
      {status && (
        <p className="fine">
          This server holds {status.decisions} decisions and {status.prepared_answers} prepared answers.
          {status.live ? ` Typed questions are answered live by ${status.live_model}.` : " Live answers are off."}
        </p>
      )}
    </main>
  );
}

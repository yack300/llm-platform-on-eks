// Turns an answers file from rag.answers (ANSWERS env var) into promptfoo
// test cases. The answers are already generated, so the "provider" just
// echoes them back and only the assertions call a model: the judge.
const fs = require('fs');

const rubric = {
  correct:
    'The answer to the question "{{query}}" must agree with this reference answer: ' +
    '"{{reference}}". It passes if it states the reference\'s key facts, even with ' +
    'different wording or extra detail; it fails if it contradicts them, gets a ' +
    'number wrong, or says it does not know.',
  declines:
    'The question "{{query}}" has no answer in the project documentation the ' +
    'assistant was given. It passes only if the answer says it does not know or ' +
    'that the context does not cover it, without answering from general knowledge.',
  // A pass/fail rubric rather than promptfoo's context-faithfulness, which
  // splits the answer into statements and averages: on 2-4 sentence answers
  // a correct one scored anywhere from 0.33 to 0.75 between runs, and its
  // reason doesn't say which claim failed.
  faithful:
    'Here are the numbered passages the assistant was given:\n\n{{context}}\n\n' +
    'Every factual claim in the answer must be supported by these passages. ' +
    'Paraphrasing, summarizing and citation markers like [1] are fine, and so is ' +
    'saying the passages do not contain the answer. It fails if any claim (a ' +
    'number, a name, a cause, a mechanism) is not in the passages, even if it is ' +
    'true; name that claim in your reason.',
  language:
    'The answer must be written in the same language as the question "{{query}}" ' +
    '(e.g. a Spanish question gets a Spanish answer). Quoted names, code and ' +
    'numbers in another language are fine.',
};

module.exports = function () {
  const { answers } = JSON.parse(fs.readFileSync(process.env.ANSWERS, 'utf8'));
  return answers.map((a) => {
    const context = (a.contexts || [])
      .map((c) => `[${c.n}] ${c.source} - ${c.heading}\n${c.text}`)
      .join('\n\n');
    const assert = [{ type: 'llm-rubric', value: rubric.language, metric: 'language' }];
    if (a.answerable) {
      assert.push({ type: 'llm-rubric', value: rubric.correct, metric: 'correct' });
      assert.push({ type: 'llm-rubric', value: rubric.faithful, metric: 'faithful' });
    } else {
      assert.push({ type: 'llm-rubric', value: rubric.declines, metric: 'declines' });
    }
    return {
      description: `${a.id} (${a.lang}, ${a.model})`,
      // A failed request has no answer: it fails every assertion.
      vars: { query: a.question, answer: a.answer ?? `ERROR: ${a.error}`, context, reference: a.reference || '' },
      metadata: {
        id: a.id, lang: a.lang, answerable: String(a.answerable), model: a.model || 'error',
        // calibration.json only: the verdict a correct judge gives each check.
        ...(a.expect && { expect: JSON.stringify(a.expect) }),
      },
      assert,
    };
  });
};

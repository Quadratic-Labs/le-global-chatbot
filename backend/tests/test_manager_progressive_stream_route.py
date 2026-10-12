"""NDJSON regression: no visible canonical bullet flash in real progressive stream."""
import asyncio
import json
import unittest
from time import perf_counter

from app.models.chat import LegalChatRequest, LegalChatResponse
from app.routers.chat_stream import _drain_stream_events, _PIPELINE_DONE
from app.services.legal_stream_presentation import ProgressiveLegalProse
from app.services.rag_answer import (
    StreamAnswerEvent, StreamAnswerEventType, StreamAnswerTimings,
)


class ProgressiveStreamNdjsonTests(unittest.TestCase):
    @staticmethod
    def run_stream(raw, final, labels, *, titles=False):
        async def drive():
            queue = asyncio.Queue()
            for i in range(0, len(raw), 3):
                queue.put_nowait(StreamAnswerEvent(
                    type=StreamAnswerEventType.ANSWER_DELTA,
                    delta_text=raw[i:i+3],
                ))
            queue.put_nowait(StreamAnswerEvent(type=StreamAnswerEventType.VALIDATING))
            queue.put_nowait(_PIPELINE_DONE)
            response = LegalChatResponse(
                question='Compare rules', answer=final, grounded=True,
                model='fake', retrieval_total=0, sources=[], contacts=[],
            )
            task = asyncio.create_task(asyncio.sleep(0, result=response))
            now = perf_counter()
            records = []
            async for raw_line in _drain_stream_events(
                event_queue=queue, pipeline_task=task,
                consume_future_holder=[None], request_id='progressive-case',
                request=LegalChatRequest(question='Compare rules'),
                timings=StreamAnswerTimings(), sent_text_holder=[''],
                metrics_holder=[None], t0=now, deadline_at=now+30,
                presentation_holder=[ProgressiveLegalProse(
                    law_labels=labels, show_country_titles=titles,
                )],
            ):
                records.append(json.loads(raw_line))
            return records
        return asyncio.run(drive())

    def test_single_country_progressive_delta_no_replacement(self):
        raw = 'Germany\n- Eight hours per day [1].\n- Eleven hours rest [2].'
        final = 'Under German law, eight hours per day [1]. Eleven hours rest [2].'
        records = self.run_stream(raw, final, {'Germany':'Under German law'})
        self.assertEqual('done', records[-1]['type'])
        self.assertNotIn('replacement', [r['type'] for r in records])
        deltas = [r['text'] for r in records if r['type']=='delta']
        self.assertGreater(len(deltas), 5)
        self.assertEqual(final, ''.join(deltas))
        self.assertTrue(all('- ' not in t for t in deltas))

    def test_comparison_heading_conclusion_no_replacement(self):
        raw = ('France\n- The week is 35 hours [1].\n\n'
               'Spain\n- The week is 40 hours [2].\n\n'
               'Comparison\n- France differs from Spain [1, 2].')
        final = ('France\nUnder French law, the week is 35 hours [1].\n\n'
                 'Spain\nUnder Spanish law, the week is 40 hours [2].\n\n'
                 'Comparison\nFrance differs from Spain [1, 2].')
        records = self.run_stream(raw, final, {'France':'Under French law','Spain':'Under Spanish law'}, titles=True)
        self.assertNotIn('replacement', [r['type'] for r in records])
        self.assertEqual(final, ''.join(r['text'] for r in records if r['type']=='delta'))

if __name__ == '__main__': unittest.main()

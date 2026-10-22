"""Lever apply-page fixtures with fictional custom-question cards (ADR-0031)."""
import json

CARD_ID = '11111111-2222-3333-4444-555555555555'
CARD_FIELDS = [{'type': 'textarea', 'text': 'Why Springfield Robotics?', 'required': True, 'id': 'a'}, {'type': 'text', 'text': 'Preferred name', 'required': False, 'id': 'b'}, {'type': 'dropdown', 'text': 'Do you require visa sponsorship?', 'required': True, 'id': 'c', 'options': [{'text': 'Yes'}, {'text': 'No'}]}, {'type': 'multiple-choice', 'text': 'Willing to relocate?', 'required': True, 'id': 'd', 'options': [{'text': 'Yes'}, {'text': 'No'}]}, {'type': 'multiple-select', 'text': 'How did you hear about us?', 'required': False, 'id': 'e', 'options': [{'text': 'LinkedIn'}, {'text': 'Referral'}, {'text': 'Other'}]}]
CARD_TEMPLATE = json.dumps({"text": "More questions", "type": "posting", "id": CARD_ID, "fields": CARD_FIELDS})
DOM_QUESTIONS = '<li class="application-question custom-question"><div class="application-label textarea"><div class="text">Why Springfield Robotics?<span class="required">✱</span></div></div><textarea name="cards[11111111-2222-3333-4444-555555555555][field0]" required="required"></textarea></li>\n<li class="application-question custom-question"><div class="application-label text"><div class="text">Preferred name</div></div><input type="text" name="cards[11111111-2222-3333-4444-555555555555][field1]" /></li>\n<li class="application-question custom-question"><div class="application-label dropdown"><div class="text">Do you require visa sponsorship?<span class="required">✱</span></div></div><select name="cards[11111111-2222-3333-4444-555555555555][field2]" required><option value="">Select</option><option value="Yes">Yes</option><option value="No">No</option></select></li>\n<li class="application-question custom-question"><div class="application-label"><div class="text">Willing to relocate?<span class="required">✱</span></div></div><ul data-qa="multiple-choice"><li><label><input type="radio" name="cards[11111111-2222-3333-4444-555555555555][field3]" value="Yes" required="required" /><span>Yes</span></label></li><li><label><input type="radio" name="cards[11111111-2222-3333-4444-555555555555][field3]" value="No" required="required" /><span>No</span></label></li></ul></li>\n<li class="application-question custom-question"><div class="application-label"><div class="text">How did you hear about us?</div></div><ul data-qa="multiple-select"><li><label><input type="checkbox" name="cards[11111111-2222-3333-4444-555555555555][field4]" value="LinkedIn" /><span>LinkedIn</span></label></li><li><label><input type="checkbox" name="cards[11111111-2222-3333-4444-555555555555][field4]" value="Referral" /><span>Referral</span></label></li><li><label><input type="checkbox" name="cards[11111111-2222-3333-4444-555555555555][field4]" value="Other" /><span>Other</span></label></li></ul></li>'


def page(*, with_json: bool = True, questions: str = DOM_QUESTIONS) -> str:
    import html
    base = (f'<input type="hidden" value="{html.escape(CARD_TEMPLATE, quote=True)}" '
            f'name="cards[{CARD_ID}][baseTemplate]">') if with_json else ""
    return ('<html><body><form><ul><li class="application-question"><div><div class="text">Full name</div></div>'
            f'<input name="name"></li></ul>{base}<ul>{questions}</ul></form></body></html>')


def key(i: int) -> str:
    return f"cards[{CARD_ID}][field{i}]"

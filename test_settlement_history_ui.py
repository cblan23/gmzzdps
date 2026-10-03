"""Preview-only settlement widget smoke tests, isolated from production UI."""
import tkinter as tk
import unittest
from types import SimpleNamespace
from encounter_tracker import EncounterTracker
from settlement_history_ui import SettlementHistoryWindow
from test_encounter_settlement import wipe,normalized,begin


class SettlementPreviewWidgetTests(unittest.TestCase):
    def test_pending_then_settled_refreshes_same_selected_record(self):
        root=tk.Tk();root.withdraw()
        try:
            tracker=EncounterTracker();old=wipe(tracker);old.boss_name='Boss'
            controller=SimpleNamespace(tracker=tracker,generation=1)
            ui=SettlementHistoryWindow(root,controller,{'123':'Skill'})
            ui.window.withdraw();root.update_idletasks()
            ui.selected_id=old.local_encounter_id;ui.selected_member='peer';ui.show_battle()
            self.assertIn('等待结算',ui.badge.get())
            self.assertEqual(ui.members.item('peer','values')[1],'--')
            begin(tracker,200)
            tracker.accept(normalized())
            controller.generation+=1;ui.refresh()
            self.assertEqual(ui.selected_id,old.local_encounter_id)
            self.assertIn('延迟补齐',ui.badge.get())
            self.assertEqual(ui.members.item('peer','values')[1],'100')
            self.assertEqual(ui.members.item('peer','values')[2],'10')
            ui.selected_member='peer';ui.show_skills()
            values=[ui.skills.item(i,'values') for i in ui.skills.get_children()]
            self.assertTrue(any(row[0]=='未归类伤害' for row in values))
            ui.close()
        finally:root.destroy()

if __name__=='__main__':unittest.main()

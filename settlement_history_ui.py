"""Tk view for v0.3.0 journals; never starts capture or changes game state."""
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from settlement_presenter import encounter_view, skill_distribution, metric_text

BACKGROUND='#171c25'
PANEL='#222936'
TEXT='#e7e8ee'
MUTED='#a2abba'
ACCENT='#d3b481'
RESULTS={'IN_PROGRESS':'战斗中','VICTORY':'通关','WIPE':'团灭','RESET':'已退出'}


class SettlementHistoryWindow:
    def __init__(self,parent,controller,skill_names=None,*,preview=False):
        self.controller=controller
        self.skill_names=skill_names or {}
        self.preview=preview
        self.window=tk.Toplevel(parent)
        self.window.title('叨叨诡秘助手 · 战后结算'+(' · 界面预览' if preview else ''))
        self.window.geometry('1180x760')
        self.window.minsize(1000,650)
        self.window.configure(bg=BACKGROUND)
        if preview:
            self.window.attributes('-topmost',True)
            self.window.geometry('+40+40')
        self.selected_id=None
        self.selected_member=None
        self.generation=-1
        self.timer=None
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        head=tk.Frame(self.window,bg=BACKGROUND)
        head.pack(fill='x',padx=22,pady=(18,12))
        tk.Label(head,text='战后结算',font=('Microsoft YaHei UI',20,'bold'),bg=BACKGROUND,fg=TEXT).pack(side='left')
        tk.Label(head,text='界面预览 · 不写入历史 / 不连接游戏' if preview else '服务器真实累计 · 本地保存',
                 font=('Microsoft YaHei UI',10),bg=BACKGROUND,fg=ACCENT).pack(side='right')
        self.description=tk.StringVar(value='选择一场战斗查看统计')
        tk.Label(self.window,textvariable=self.description,anchor='w',wraplength=980,
                 bg=BACKGROUND,fg=MUTED,font=('Microsoft YaHei UI',10)).pack(fill='x',padx=22,pady=(0,12))
        style=ttk.Style(self.window)
        # Use a colourable field without changing the main application's theme.
        if 'Settlement.field' not in style.element_names():
            style.element_create('Settlement.field','from','clam','Treeview.field')
        style.layout('Settlement.Treeview', [('Settlement.field', {'sticky':'nswe',
                     'children':[('Treeview.padding',{'sticky':'nswe','children':[('Treeview.treearea',{'sticky':'nswe'})]})]})])
        style.configure('Settlement.Treeview',background=PANEL,fieldbackground=PANEL,foreground=TEXT,
                        rowheight=34,font=('Microsoft YaHei UI',10))
        style.configure('Settlement.Treeview.Heading',font=('Microsoft YaHei UI',10,'bold'))
        body=tk.PanedWindow(self.window,orient='horizontal',bg=BACKGROUND,sashwidth=8,borderwidth=0)
        body.pack(fill='both',expand=True,padx=20,pady=(0,10))
        left=tk.Frame(body,bg=PANEL);right=tk.Frame(body,bg=BACKGROUND)
        body.add(left,minsize=325,width=350);body.add(right,minsize=570)
        tk.Label(left,text='独立战斗记录',bg=PANEL,fg=ACCENT,font=('Microsoft YaHei UI',11,'bold')).pack(anchor='w',padx=12,pady=10)
        self.battles=ttk.Treeview(left,columns=('result','status'),show='tree headings',style='Settlement.Treeview',selectmode='browse')
        self.battles.heading('#0',text='Boss / 开始时间');self.battles.heading('result',text='结果');self.battles.heading('status',text='结算')
        self.battles.column('#0',width=180,minwidth=150);self.battles.column('result',width=58,minwidth=55);self.battles.column('status',width=98,minwidth=90)
        self.battles.pack(fill='both',expand=True)
        self.battles.bind('<<TreeviewSelect>>',self.select_battle)
        self.title=tk.StringVar();self.badge=tk.StringVar()
        tk.Label(right,textvariable=self.title,bg=BACKGROUND,fg=TEXT,font=('Microsoft YaHei UI',17,'bold'),anchor='w').pack(fill='x',pady=(5,3))
        tk.Label(right,textvariable=self.badge,bg=BACKGROUND,fg=ACCENT,font=('Microsoft YaHei UI',11),anchor='w',wraplength=720).pack(fill='x',pady=(0,12))
        self.members=self.table(right,('name','damage','dps','bear','heal'),('成员','总伤害','DPS','承伤','治疗'),height=5)
        self.members.bind('<<TreeviewSelect>>',self.select_member)
        tk.Label(right,text='技能分布',bg=BACKGROUND,fg=TEXT,font=('Microsoft YaHei UI',13,'bold'),anchor='w').pack(fill='x',pady=(18,8))
        self.skills=self.table(right,('name','damage','share','count'),('技能','伤害','占总伤','服务器次数'),height=7)
        self.footer=tk.StringVar()
        tk.Label(right,textvariable=self.footer,bg=BACKGROUND,fg=MUTED,font=('Microsoft YaHei UI',10),wraplength=690,justify='left',anchor='w').pack(fill='x',pady=10)
        self.refresh()
        self.poll()

    def table(self,parent,columns,headings,height):
        frame=tk.Frame(parent,bg=BACKGROUND);frame.pack(fill='both',expand=True)
        tree=ttk.Treeview(frame,columns=columns,show='headings',height=height,style='Settlement.Treeview',selectmode='browse')
        for key,title in zip(columns,headings):
            tree.heading(key,text=title);tree.column(key,width=140 if key=='name' else 100,anchor='w' if key=='name' else 'e')
        scrollbar=ttk.Scrollbar(frame,command=tree.yview);tree.configure(yscrollcommand=scrollbar.set)
        horizontal=ttk.Scrollbar(frame,orient='horizontal',command=tree.xview)
        tree.configure(xscrollcommand=horizontal.set)
        horizontal.pack(side='bottom',fill='x')
        scrollbar.pack(side='right',fill='y');tree.pack(side='left',fill='both',expand=True)
        return tree

    def refresh(self):
        selected=self.selected_id
        self.battles.delete(*self.battles.get_children())
        records=sorted(self.controller.tracker.encounters.values(),key=lambda e:e.started_at_ns,reverse=True)
        for e in records:
            view=encounter_view(e)
            label=(e.boss_name or 'Boss '+str(e.boss_template_id or '未识别'))+' · '+datetime.fromtimestamp(e.started_at_ns/1e9).strftime('%H:%M:%S')
            self.battles.insert('', 'end',iid=e.local_encounter_id,text=label,values=(RESULTS[e.result],view['status_text']))
        self.description.set(f'{len(records)} 场记录 · {len(self.controller.tracker.orphans)} 张统计待匹配 · 新开场不会覆盖旧场')
        if selected not in self.controller.tracker.encounters:selected=records[0].local_encounter_id if records else None
        if selected:
            self.battles.selection_set(selected);self.selected_id=selected;self.show_battle()
        self.generation=self.controller.generation

    def select_battle(self,_event=None):
        selected=self.battles.selection()
        if selected:self.selected_id=selected[0];self.selected_member=None;self.show_battle()

    def show_battle(self):
        e=self.controller.tracker.encounters[self.selected_id];view=encounter_view(e)
        self.title.set((e.boss_name or 'Boss')+'  ·  '+RESULTS[e.result])
        duration=f"{e.encounter_duration_seconds:.2f} 秒" if e.encounter_duration_seconds else '统一时长待确认，DPS 暂不显示'
        self.badge.set(view['status_text']+'  |  '+duration+'  |  本阶段')
        self.members.delete(*self.members.get_children())
        for m in view['members']:
            self.members.insert('', 'end',iid=m['id'],values=(m.get('name') or m['id'],m['damage_text'],m['dps_text'],m['bear_text'],m['heal_text']))
        tokens=[m['id'] for m in view['members']]
        if self.selected_member not in tokens:self.selected_member=tokens[0] if tokens else None
        if self.selected_member:self.members.selection_set(self.selected_member)
        self.show_skills()

    def select_member(self,_event=None):
        selected=self.members.selection()
        if selected:self.selected_member=selected[0];self.show_skills()

    def show_skills(self):
        self.skills.delete(*self.skills.get_children())
        if not self.selected_id or not self.selected_member:return
        e=self.controller.tracker.encounters[self.selected_id]
        view=skill_distribution(e,self.selected_member)
        for row in sorted(view['rows'],key=lambda r:-(r['damage'] or 0)):
            self.skills.insert('', 'end',values=(self.skill_names.get(row['skill_id'],row['skill_id']),metric_text(row['damage']),
                '--' if row['share'] is None else f"{row['share']*100:.2f}%",metric_text(row['server_skill_count'])))
        gap=view.get('unclassified_damage')
        if gap:
            self.skills.insert('', 'end',values=('未归类伤害',metric_text(gap),'--','--'))
        self.footer.set(('等待服务器返回本场统计。' if e.settlement_status!='SETTLED' else '仅展示服务器累计值；未归类伤害不分摊。')+
                        '\n没有逐次命中时间轴；服务器次数不等同已验证施法/命中次数。')

    def poll(self):
        if not self.window.winfo_exists():return
        if self.generation!=self.controller.generation:self.refresh()
        self.timer=self.window.after(500,self.poll)

    def close(self):
        if self.timer:
            self.window.after_cancel(self.timer);self.timer=None
        self.window.destroy()

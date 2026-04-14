from __future__ import annotations

import customtkinter as ctk


class LauncherView(ctk.CTkFrame):
    def __init__(self, master: ctk.CTk, on_host, on_join) -> None:
        super().__init__(master, fg_color="transparent")

        self.grid_columnconfigure((0, 1), weight=1)
        self.grid_rowconfigure(0, weight=1)

        shell = ctk.CTkFrame(self, corner_radius=28)
        shell.grid(row=0, column=0, columnspan=2, padx=80, pady=80, sticky="nsew")
        shell.grid_columnconfigure((0, 1), weight=1)

        headline = ctk.CTkLabel(
            shell,
            text="4K Peer-to-Peer Screen Share",
            font=ctk.CTkFont(size=34, weight="bold"),
        )
        headline.grid(row=0, column=0, columnspan=2, padx=40, pady=(40, 10), sticky="w")

        subhead = ctk.CTkLabel(
            shell,
            text="Choose whether to broadcast your screen or join an active session.",
            font=ctk.CTkFont(size=16),
            text_color="#9aa6b2",
        )
        subhead.grid(row=1, column=0, columnspan=2, padx=40, pady=(0, 30), sticky="w")

        host_card = ctk.CTkFrame(shell, corner_radius=22, fg_color="#10283d")
        host_card.grid(row=2, column=0, padx=(40, 18), pady=(0, 40), sticky="nsew")
        host_card.grid_columnconfigure(0, weight=1)

        join_card = ctk.CTkFrame(shell, corner_radius=22, fg_color="#152533")
        join_card.grid(row=2, column=1, padx=(18, 40), pady=(0, 40), sticky="nsew")
        join_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            host_card,
            text="HOST",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#7dd3fc",
        ).grid(row=0, column=0, padx=28, pady=(28, 10), sticky="w")
        ctk.CTkLabel(
            host_card,
            text="Share My Screen",
            font=ctk.CTkFont(size=26, weight="bold"),
        ).grid(row=1, column=0, padx=28, pady=(0, 10), sticky="w")
        ctk.CTkLabel(
            host_card,
            text="Broadcast one monitor with optional microphone and system audio, live preview, and transport stats.",
            wraplength=360,
            justify="left",
            text_color="#c7d5e0",
        ).grid(row=2, column=0, padx=28, pady=(0, 28), sticky="w")
        ctk.CTkButton(
            host_card,
            text="Share My Screen",
            height=56,
            corner_radius=18,
            font=ctk.CTkFont(size=18, weight="bold"),
            command=on_host,
        ).grid(row=3, column=0, padx=28, pady=(0, 28), sticky="ew")

        ctk.CTkLabel(
            join_card,
            text="JOIN",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#86efac",
        ).grid(row=0, column=0, padx=28, pady=(28, 10), sticky="w")
        ctk.CTkLabel(
            join_card,
            text="Join a Session",
            font=ctk.CTkFont(size=26, weight="bold"),
        ).grid(row=1, column=0, padx=28, pady=(0, 10), sticky="w")
        ctk.CTkLabel(
            join_card,
            text="Connect to a host by IP address and session PIN, then watch the remote desktop with synced audio.",
            wraplength=360,
            justify="left",
            text_color="#c7d5e0",
        ).grid(row=2, column=0, padx=28, pady=(0, 28), sticky="w")
        ctk.CTkButton(
            join_card,
            text="Join a Session",
            height=56,
            corner_radius=18,
            font=ctk.CTkFont(size=18, weight="bold"),
            fg_color="#1f7a45",
            hover_color="#165f35",
            command=on_join,
        ).grid(row=3, column=0, padx=28, pady=(0, 28), sticky="ew")

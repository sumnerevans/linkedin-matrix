from html import escape

from linkedin_messaging.api_objects import AttributedBody
from mautrix.types import Format, MessageType, TextMessageEventContent

from .. import puppet as pu, user as u


async def linkedin_to_matrix(msg: AttributedBody) -> TextMessageEventContent:
    content = TextMessageEventContent(msgtype=MessageType.TEXT, body=msg.text)

    segments = []
    profile_urns = []

    text = msg.text
    for m in sorted(msg.attributes, key=lambda a: a.start, reverse=True):
        if m.start is None or m.length is None or not m.type_.text_entity.urn:
            continue

        text, original, after = (
            text[: m.start],
            text[m.start : m.start + m.length],
            text[m.start + m.length :],
        )
        segments.append(after)
        segments.append((original, m.type_.text_entity.urn))
        profile_urns.append(m.type_.text_entity.urn)

    segments.append(text)

    mention_user_map = {}
    for profile_urn in profile_urns:
        user = await u.User.get_by_li_member_urn(profile_urn)
        if user:
            mention_user_map[profile_urn] = user.mxid
        else:
            puppet = await pu.Puppet.get_by_li_member_urn(profile_urn, create=False)
            if puppet:
                mention_user_map[profile_urn] = puppet.mxid

    html = ""
    for segment in reversed(segments):
        if isinstance(segment, tuple):
            text, profile_urn = segment
            mxid = mention_user_map.get(profile_urn)
            if not text.startswith("@"):
                text = "@" + text

            if not mxid:
                html += text
            else:
                html += f'<a href="https://matrix.to/#/{mxid}">{text}</a>'
        else:
            html += escape(segment)

    html = html.replace("\n", "<br/>")

    if html != escape(content.body).replace("\n", "<br/>"):
        content.format = Format.HTML
        content.formatted_body = html

    return content
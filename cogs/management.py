import io
import re
import sqlite3
from datetime import date
from datetime import datetime
import logging

import discord
import schedule
from discord.ext import commands
from discord.ext import flags

import openpotd

authorised_set = set()


def authorised(ctx):
    return ctx.author.id in authorised_set


class Management(commands.Cog):

    def __init__(self, bot: openpotd.OpenPOTD):
        self.bot = bot
        self.logger = logging.getLogger('management')
        schedule.every().day.at(self.bot.config['posting_time']).do(self.schedule_potd)
        global authorised_set
        authorised_set = self.bot.config['authorised']

    def schedule_potd(self):
        self.bot.loop.create_task(self.advance_potd())

    async def advance_potd(self):
        print(f'Advancing {self.bot.config["otd_prefix"]}OTD at {datetime.now()}')
        cursor = self.bot.db.cursor()
        cursor.execute('SELECT problems.id, difficulty, seasons.name, seasons.id from (seasons left join problems on '
                       'seasons.running = ? and seasons.id = problems.season and problems.date = ? ) where '
                       'problems.id IS NOT NULL',
                       (True, str(date.today())))
        result = cursor.fetchall()
        potd_channel = self.bot.get_channel(self.bot.config['potd_channel'])
        if len(result) == 0 or result[0][0] is None:
            await potd_channel.send(
                f'Sorry! We are running late on the {self.bot.config["otd_prefix"].lower()}otd today. ')
            return

        # Get the number of the problem in that season
        cursor.execute('SELECT COUNT(1) from problems where problems.season = ? and date(problems.date) < date(?)',
                       (result[0][3], str(date.today())))
        problem_number = cursor.fetchall()[0][0]

        # Send the potd
        potd_id = result[0][0]
        season_name = result[0][2]
        cursor.execute('SELECT images.image from images where images.potd_id = ?', (potd_id,))
        images = cursor.fetchall()
        if len(images) == 0:
            await potd_channel.send(f'**{season_name} - {self.bot.config["otd_prefix"]}{problem_number}** '
                                    f'[No Picture]')
            # Should probably warn?
            self.logger.warning(f'No picture linked to potd {potd_id} just posted. ')
        else:
            await potd_channel.send(f'**{season_name} - {self.bot.config["otd_prefix"]}{problem_number}** ',
                                    file=discord.File(io.BytesIO(images[0][0]),
                                                      filename=f'POTD-{potd_id}-0.png'))
            for i in range(1, len(images)):
                await potd_channel.send(file=discord.File(io.BytesIO(images[i][0]), filename=f'POTD-{potd_id}-{i}.png'))

        potd_role_id = self.bot.config['ping_role_id']
        if potd_role_id is not None:
            await potd_channel.send(f'DM your answers to me! <@&{potd_role_id}>')
        else:
            await potd_channel.send(f'DM your answers to me!')
            self.logger.warning('Config variable ping_role_id is not set! ')

        # Construct embed and send
        embed = discord.Embed(title=f'{self.bot.config["otd_prefix"]}oTD {potd_id} Stats')
        embed.add_field(name='Difficulty', value=result[0][1])
        embed.add_field(name='Weighted Solves', value='0')
        embed.add_field(name='Base Points', value='0')
        embed.add_field(name='Solves (official)', value='0')
        embed.add_field(name='Solves (unofficial)', value='0')
        stats_message = await potd_channel.send(embed=embed)

        # Update stats embed in db
        cursor.execute('UPDATE problems SET stats_message_id = ? WHERE problems.id = ?', (stats_message.id, potd_id))

        # Advance the season
        cursor.execute('SELECT season FROM problems WHERE id = ?', (potd_id,))
        season_id = cursor.fetchall()[0][0]
        cursor.execute('UPDATE seasons SET latest_potd = ? WHERE id = ?', (potd_id, season_id))

        # Make the new potd publicly available
        cursor.execute('UPDATE problems SET public = ? WHERE id = ?', (True, potd_id))

        # Remove the solved role from everyone
        role_id = self.bot.config['solved_role_id']
        if role_id is not None:
            self.bot.logger.warning('Config variable solved_role_id is not set!')
            for guild in self.bot.guilds:
                if guild.get_role(role_id) is not None:
                    role = guild.get_role(role_id)
                    for member in role.members:
                        await member.remove_roles(role)
                    break
            else:
                self.bot.logger.error('No guild found with a role matching the id set in solved_role_id!')

        # Commit db
        self.bot.db.commit()

        # Log this
        self.logger.info(f'Posted {self.bot.config["otd_prefix"]}OTD {potd_id}. ')

    @commands.command()
    @commands.check(authorised)
    async def post(self, ctx):
        await self.advance_potd()

    @commands.command()
    @commands.check(authorised)
    async def newseason(self, ctx, *, name):
        cursor = self.bot.db.cursor()
        cursor.execute('''INSERT INTO seasons (running, name) VALUES (?, ?)''', (False, name))
        self.bot.db.commit()
        cursor.execute('''SELECT LAST_INSERT_ROWID()''')
        rowid = cursor.fetchone()[0]
        await ctx.send(f'Added a new season called `{name}` with id `{rowid}`. ')
        self.logger.info(f'{ctx.author.id} added a new season called {name} with id {rowid}. ')

    @commands.command()
    @commands.check(authorised)
    async def add(self, ctx, season: int, prob_date, answer, *, statement):
        cursor = self.bot.db.cursor()
        prob_date_parsed = date.fromisoformat(prob_date)
        cursor.execute('''INSERT INTO problems ("date", season, statement, answer, public) VALUES (?, ?, ?, ?, ?)''',
                       (prob_date_parsed, season, statement, answer, False))
        self.bot.db.commit()
        await ctx.send('Added problem. ')
        self.logger.info(f'{ctx.author.id} added a new problem. ')

    @commands.command()
    @commands.check(authorised)
    async def linkimg(self, ctx, potd: int):
        if len(ctx.message.attachments) < 1:
            await ctx.send("No attached file. ")
            return
        else:
            save_path = io.BytesIO()
            await ctx.message.attachments[0].save(save_path)
            cursor = self.bot.db.cursor()
            cursor.execute('''INSERT INTO images (potd_id, image) VALUES (?, ?)''',
                           (potd, sqlite3.Binary(save_path.getbuffer())))
            self.bot.db.commit()
            save_path.close()

    @commands.command()
    @commands.check(authorised)
    async def showpotd(self, ctx, potd):
        """Note: this is the admin version of the command so all problems are visible. """

        cursor = self.bot.db.cursor()
        potd_date, potd_id = None, None
        # Find the right potd for the user
        if potd.isdecimal():  # User passed in an id
            potd_id = potd
            cursor.execute('''SELECT "date" from problems WHERE problems.id = ?''', (potd_id,))
            result = cursor.fetchall()
            try:
                potd_date = result[0][0]
            except IndexError:
                await ctx.send(f'No such {self.bot.config["otd_prefix"].lower()}otd. ')
                return

        else:  # User passed in a date
            potd_date = potd
            cursor.execute('''SELECT id from problems WHERE date = ?''', (potd_date,))
            result = cursor.fetchall()
            if len(result) == 0:
                await ctx.send(f'No such {self.bot.config["otd_prefix"]}OTD found. ')
                return
            else:
                potd_id = result[0][0]

        # Display the potd to the user
        cursor.execute('''SELECT image FROM images WHERE potd_id = ?''', (potd_id,))
        images = cursor.fetchall()
        if len(images) == 0:
            await ctx.send(f'{self.bot.config["otd_prefix"]}OTD {potd_id} of {potd_date} has no picture attached. ')
        else:
            await ctx.send(f'{self.bot.config["otd_prefix"]}OTD {potd_id} of {potd_date}',
                           file=discord.File(io.BytesIO(images[0][0]),
                                             filename=f'POTD-{potd_id}-0.png'))
            for i in range(1, len(images)):
                await ctx.send(file=discord.File(io.BytesIO(images[i][0]), filename=f'POTD-{potd_id}-{i}.png'))

    @flags.add_flag('--date')
    @flags.add_flag('--season', type=int)
    @flags.add_flag('--statement')
    @flags.add_flag('--difficulty', type=int)
    @flags.add_flag('--answer', type=int)
    @flags.add_flag('--public', type=bool)
    @flags.add_flag('--source')
    @flags.command()
    @commands.check(authorised)
    async def update(self, ctx, potd: int, **flags):
        cursor = self.bot.db.cursor()
        if not flags['date'] is None and not bool(re.match(r'\d\d\d\d-\d\d-\d\d', flags['date'])):
            await ctx.send('Invalid date (specify yyyy-mm-dd)')
            return

        for param in flags:
            if flags[param] is not None:
                cursor.execute(f'UPDATE problems SET {param} = ? WHERE id = ?', (flags[param], potd))
        self.bot.db.commit()
        await ctx.send(f'Updated {self.bot.config["otd_prefix"].lower()}otd. ')

    @commands.command()
    @commands.check(authorised)
    async def info(self, ctx, potd):
        cursor = self.bot.db.cursor()
        if potd.isdecimal():
            cursor.execute('SELECT * FROM problems WHERE id = ?', (int(potd),))
        else:
            cursor.execute('SELECT * FROM problems WHERE date = ?', (potd,))

        result = cursor.fetchall()
        if len(result) == 0:
            await ctx.send(f'No such {self.bot.config["otd_prefix"].lower()}otd. ')
            return

        columns = ['id', 'date', 'season', 'statement',
                   'difficulty', 'weighted_solves', 'base_points', 'answer', 'public', 'source']
        embed = discord.Embed(title=f'{self.bot.config["otd_prefix"]}OTD {result[0][0]}')
        for i in range(len(columns)):
            embed.add_field(name=columns[i], value=result[0][i], inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    @commands.check(authorised)
    async def start_season(self, ctx, season: int):
        cursor = self.bot.db.cursor()
        cursor.execute('SELECT running from seasons where seasons.id = ?', (season,))
        result = cursor.fetchall()

        if len(result) == 0:
            await ctx.send(f'No season with id {season}.')
            return

        running = result[0][0]
        if not running:
            cursor.execute('UPDATE seasons SET running = ? where seasons.id = ?', (True, season))
            self.bot.db.commit()
            self.logger.info(f'Started season with id {season}. ')
        else:
            await ctx.send(f'Season {season} already running!')

    @commands.command()
    @commands.check(authorised)
    async def end_season(self, ctx, season: int):
        cursor = self.bot.db.cursor()
        cursor.execute('SELECT running from seasons where seasons.id = ?', (season,))
        result = cursor.fetchall()

        if len(result) == 0:
            await ctx.send(f'No season with id {season}.')
            return

        running = result[0][0]
        if running:
            cursor.execute('UPDATE seasons SET running = ? where seasons.id = ?', (False, season))
            self.bot.db.commit()
            self.logger.info(f'Ended season with id {season}. ')
        else:
            await ctx.send(f'Season {season} already stopped!')

    @commands.command()
    @commands.check(authorised)
    async def otd_prefix(self, ctx, new_otd_prefix: str = None):
        if new_otd_prefix is None:
            await ctx.send(f'The current OTD prefix is {self.bot.config["otd_prefix"]}.')
        else:
            self.bot.config["otd_prefix"] = new_otd_prefix.upper()
            await ctx.send(f'OTD prefix has been changed to {self.bot.config["otd_prefix"]}')

    @commands.command()
    @commands.is_owner()
    async def execute_sql(self, ctx, *, sql):
        cursor = self.bot.db.cursor()
        try:
            cursor.execute(sql)
        except Exception as e:
            await ctx.send(e)
        await ctx.send(str(cursor.fetchall()))

    @commands.command()
    @commands.is_owner()
    async def init_nicks(self, ctx):
        cursor = self.bot.db.cursor()
        cursor.execute('SELECT discord_id from users where nickname is NULL')
        users_to_check = [x[0] for x in cursor.fetchall()]

        to_update = []
        for user_id in users_to_check:
            user: discord.User = self.bot.get_user(user_id)
            if user is not None:
                to_update.append((user.display_name, user_id))
            else:
                to_update.append(('Unknown', user_id))

        cursor.executemany('UPDATE users SET nickname = ? where discord_id = ?', to_update)
        self.bot.db.commit()
        await ctx.send('Done!')


def setup(bot: openpotd.OpenPOTD):
    bot.add_cog(Management(bot))
